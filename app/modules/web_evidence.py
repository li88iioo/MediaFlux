"""公开网页搜索与正文读取的共用业务服务；返回普通 JSON 数据。"""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import re
import time
import unicodedata
from collections.abc import Callable, Iterable
from copy import deepcopy
from datetime import date
from itertools import chain
from typing import Any, cast
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup, NavigableString, Tag

from app.repositories import agent_web_search as agent_web_search_repo
from app.clients.http import (
    FixedHostHttpClient, HttpError, HttpInvalidResponse, HttpResponse,
    HttpResponseTooLarge, HttpSecurityError,
)
from app.concurrency import (
    AsyncBridgeUnavailable, KeyedSingleFlight,
    ensure_sync_bridge_available, run_awaitable_sync,
)
from app.config import get
from app.logger import get_logger
from app.sensitive_data import contains_sensitive_credential, is_sensitive_key, redact_sensitive_text

logger = get_logger(__name__)

WEB_REQUEST_SINGLE_FLIGHT_WAIT_SECONDS = 35.0
web_request_singleflight = KeyedSingleFlight(max_entries=256)
_CACHE_KIND = "web_evidence_v1"

_TAVILY_ENDPOINT = "https://api.tavily.com/search"
_ALLOWED_TOPICS = frozenset({"general", "news"})
_ALLOWED_TIME_RANGES = frozenset({"day", "week", "month", "year"})
_SEARCH_FILTERS = ("time_range", "start_date", "end_date", "include_domains", "exclude_domains")
_SEARCH_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

_READ_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_REDIRECT_CODES = {301, 302, 303, 307, 308}
_MAX_REDIRECTS = 3
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_HTML_TYPES = {"text/html", "application/xhtml+xml"}
_TEXT_TYPES = {"text/plain", "text/markdown", "text/x-markdown"}
_LOADING_TEXT_RE = re.compile(r"(?:loading|加载中|正在加载)[.\s…。]*", re.IGNORECASE)
_MARKDOWN_PREFIX_RE = re.compile(r"^[#>*+\-\s\d.)]+")
_LOCAL_HOST_SUFFIXES = (
    ".localhost",
    ".local",
    ".internal",
    ".home.arpa",
    ".lan",
)

_DEFAULT_MAX_CHARS = 10_000
_MIN_MAX_CHARS = 2_000
MAX_WEB_READ_CHARS = 2_000_000


class WebEvidenceError(ValueError):
    """脚本和展示适配可区分的业务参数错误。"""

    def __init__(self, message: str, *, code: str = "invalid_arguments") -> None:
        super().__init__(message)
        self.code = code


def _result(ok: bool, status: str, message: str, *, data: dict | None = None, error: str = "") -> dict[str, Any]:
    return {"ok": ok, "status": status, "message": message, "data": data or {}, "error": error}


def _cached_result(key: str) -> dict[str, Any] | None:
    payload = agent_web_search_repo.get_agent_web_search_cache(key)
    if not isinstance(payload, dict) or payload.get("kind") != _CACHE_KIND:
        return None
    result = payload.get("result")
    if (not isinstance(result, dict) or result.get("ok") is not True
            or not isinstance(result.get("data"), dict)
            or any(not isinstance(result.get(key), str) for key in ("status", "message", "error"))):
        return None
    result = deepcopy(result)
    result["data"]["cached"] = True
    return result


def _store_cached_result(key: str, result: dict[str, Any], *, ttl_key: str) -> None:
    ttl = _int_config(ttl_key, 900, minimum=30, maximum=86_400)
    agent_web_search_repo.set_agent_web_search_cache(key, {"kind": _CACHE_KIND, "result": result}, ttl_seconds=ttl)


def clear_web_cache() -> None:
    """失效搜索与正文缓存，并释放本进程中等待相同请求的调用方。"""
    web_request_singleflight.clear(clear_cache=agent_web_search_repo.clear_agent_web_search_cache)



def _int_config(name: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(str(get(name, str(default)) or default).strip())
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


def _search_enabled() -> bool:
    return (get("WEB_SEARCH_ENABLED", "0") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _read_enabled() -> bool:
    return (get("WEB_READ_ENABLED", "1") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def normalize_search_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise WebEvidenceError("工具参数必须是 JSON 对象")
    allowed = {"query", "max_results", "topic", *_SEARCH_FILTERS}
    extra = sorted(set(arguments) - allowed)
    if extra:
        raise WebEvidenceError(f"不支持的工具参数：{', '.join(extra)}")
    query = arguments.get("query")
    if not isinstance(query, str):
        raise WebEvidenceError("query 必须是字符串")
    query = unicodedata.normalize("NFKC", query).strip()
    if not 1 <= len(query) <= 200 or _SEARCH_CONTROL_RE.search(query):
        raise WebEvidenceError("搜索关键词必须为 1 到 200 个可见字符")
    if contains_sensitive_credential(query):
        raise WebEvidenceError(
            "搜索关键词疑似包含凭据，已拒绝发送到外部网页搜索服务",
            code="sensitive_external_input",
        )
    max_results = arguments.get("max_results", 5)
    if isinstance(max_results, bool) or not isinstance(max_results, int):
        raise WebEvidenceError("max_results 必须是整数")
    if not 1 <= max_results <= 10:
        raise WebEvidenceError("max_results 必须在 1 到 10 之间")
    topic = arguments.get("topic", "general")
    if not isinstance(topic, str) or topic.strip().lower() not in _ALLOWED_TOPICS:
        raise WebEvidenceError("topic 仅支持 general 或 news")
    normalized: dict[str, Any] = {
        "query": query,
        "max_results": max_results,
        "topic": topic.strip().lower(),
    }
    if "time_range" in arguments and arguments.get("time_range") not in (None, ""):
        time_range = arguments.get("time_range")
        if (
            not isinstance(time_range, str)
            or time_range.strip().lower() not in _ALLOWED_TIME_RANGES
        ):
            raise WebEvidenceError("time_range 仅支持 day、week、month 或 year")
        normalized["time_range"] = time_range.strip().lower()
    for key in ("start_date", "end_date"):
        value = arguments.get(key)
        if value is None or value == "":
            continue
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise WebEvidenceError(f"{key} 必须是 YYYY-MM-DD 日期")
        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise WebEvidenceError(f"{key} 必须是有效日期") from exc
        normalized[key] = value
    if "time_range" in normalized and ("start_date" in normalized or "end_date" in normalized):
        raise WebEvidenceError("time_range 与 start_date/end_date 不能同时使用")
    if normalized.get("start_date", "") > normalized.get("end_date", "9999-12-31"):
        raise WebEvidenceError("start_date 不能晚于 end_date")
    for key, limit in (("include_domains", 300), ("exclude_domains", 150)):
        domains = _normalize_domains(arguments.get(key), key, limit)
        if domains:
            normalized[key] = domains
    if set(normalized.get("include_domains", [])) & set(normalized.get("exclude_domains", [])):
        raise WebEvidenceError("同一域名不能同时包含和排除")
    return normalized


def _normalize_domains(value: Any, name: str, limit: int) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > limit:
        raise WebEvidenceError(f"{name} 必须是最多 {limit} 个域名的列表")
    domains = set()
    for item in value:
        if not isinstance(item, str) or not re.fullmatch(r"[\w.-]+", item.strip()):
            raise WebEvidenceError(f"{name} 只接受域名，如 jellyfin.org，不含协议、路径或通配符")
        url = normalize_public_url("https://" + item.strip().rstrip("."))
        host = httpx.URL(url).raw_host.decode("ascii").lower()
        if any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in host.split(".")):
            raise WebEvidenceError(f"{name} 包含无效域名")
        domains.add(host)
    return sorted(domains)


def normalize_public_url(value: Any) -> str:
    if not isinstance(value, str):
        raise WebEvidenceError("url 必须是字符串")
    raw = value.strip()
    if not 1 <= len(raw) <= 2_000 or _READ_CONTROL_RE.search(raw):
        raise WebEvidenceError("网页地址必须为 1 到 2000 个可见字符")
    if contains_sensitive_credential(raw):
        raise WebEvidenceError(
            "网页地址疑似包含凭据或签名参数，已拒绝访问该地址",
            code="sensitive_external_input",
        )
    try:
        parsed = httpx.URL(raw)
    except (TypeError, ValueError) as exc:
        raise WebEvidenceError("网页地址格式无效") from exc
    if parsed.scheme.casefold() != "https":
        raise WebEvidenceError("网页读取仅支持公开 HTTPS 地址")
    if parsed.username or parsed.password:
        raise WebEvidenceError("网页地址不得包含用户名或密码")
    host = (parsed.host or "").strip().rstrip(".").casefold()
    if not host:
        raise WebEvidenceError("网页地址缺少主机名")
    if host in {"localhost", "localhost.localdomain"} or host.endswith(
        _LOCAL_HOST_SUFFIXES
    ):
        raise WebEvidenceError("网页读取不允许访问本机或内部网络地址")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None:
        if not address.is_global:
            raise WebEvidenceError("网页读取不允许访问非公网地址")
    elif "." not in host:
        raise WebEvidenceError("网页读取只允许公开互联网主机")
    try:
        port = parsed.port
    except ValueError as exc:
        raise WebEvidenceError("网页地址端口无效") from exc
    if port not in {None, 443}:
        raise WebEvidenceError("网页读取不允许访问非常规端口")
    normalized = str(parsed.copy_with(fragment=None))
    if len(normalized) > 2_000:
        raise WebEvidenceError("网页地址过长")
    return normalized


def normalize_read_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise WebEvidenceError("工具参数必须是 JSON 对象")
    allowed = {"url", "max_chars"}
    extra = sorted(set(arguments) - allowed)
    if extra:
        raise WebEvidenceError(f"不支持的工具参数：{', '.join(extra)}")
    if "url" not in arguments:
        raise WebEvidenceError("读取网页需要 url")
    max_chars = arguments.get("max_chars", _DEFAULT_MAX_CHARS)
    if isinstance(max_chars, bool) or not isinstance(max_chars, int):
        raise WebEvidenceError("max_chars 必须是整数")
    if not _MIN_MAX_CHARS <= max_chars <= MAX_WEB_READ_CHARS:
        raise WebEvidenceError(
            f"max_chars 必须在 {_MIN_MAX_CHARS} 到 {MAX_WEB_READ_CHARS} 之间"
        )
    return {
        "url": normalize_public_url(arguments["url"]),
        "max_chars": max_chars,
    }


def _safe_search_text(value: Any, maximum: int | None = None) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = " ".join(_SEARCH_CONTROL_RE.sub(" ", text).split())
    return text if maximum is None else text[:maximum]


def _safe_search_url(value: Any) -> str:
    raw = str(value or "").strip()
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return ""
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return ""
    if parsed.username or parsed.password:
        return ""
    host = parsed.hostname.rstrip(".").lower()
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".localhost"):
        return ""
    try:
        if not ipaddress.ip_address(host).is_global:
            return ""
    except ValueError:
        pass
    try:
        port = parsed.port
    except ValueError:
        return ""
    if port is not None and port not in {80, 443}:
        return ""
    return urlunsplit(
        (parsed.scheme.lower(), parsed.netloc, parsed.path, parsed.query, "")
    )


def _search_cache_key(arguments: dict[str, Any], depth: str) -> str:
    encoded = json.dumps(
        [{**arguments, "query": arguments["query"].casefold()}, depth],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(b"mediaflux-web-search-cache:v3\0" + encoded).hexdigest()


def _read_cache_key(arguments: dict[str, Any]) -> str:
    encoded = json.dumps(
        [arguments["url"], arguments["max_chars"]],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(b"mediaflux-web-read-cache:local-v7\0" + encoded).hexdigest()


def _provider_error(status_code: int) -> dict[str, Any]:
    if status_code == 429:
        return _result(False, "rate_limited", "网页搜索服务请求过于频繁，请稍后重试")
    if status_code in {401, 403}:
        return _result(False, "authentication", "网页搜索凭据无效或无权访问")
    return _result(False, "unavailable", "网页搜索服务暂时不可用")


def _http_error(status_code: int) -> dict[str, Any]:
    if status_code == 429:
        return _result(False, "rate_limited", "目标网站限制访问频率，请稍后重试")
    if status_code in {401, 403}:
        return _result(False, "authentication", "目标网站要求登录或拒绝访问；本地读取不使用登录态或绕过验证")
    if status_code in {400, 404, 410, 422}:
        return _result(False, "unreadable", "目标网页当前无法读取")
    return _result(False, "unavailable", "目标网站暂时不可用")


def _map_search_response(payload: Any, arguments: dict[str, Any], elapsed_ms: int) -> dict[str, Any]:
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        return _result(False, "invalid_response", "网页搜索服务返回了无法识别的数据")
    results = []
    selected = payload["results"][:arguments["max_results"]]
    for raw in selected:
        if not isinstance(raw, dict):
            continue
        title = _safe_search_text(raw.get("title"))
        snippet = _safe_search_text(raw.get("content"))
        url = _safe_search_url(raw.get("url"))
        if not title or not snippet or not url:
            continue
        host = (urlsplit(url).hostname or "").lower()
        included = arguments.get("include_domains", [])
        excluded = arguments.get("exclude_domains", [])
        if (included and not any(host == domain or host.endswith("." + domain) for domain in included)
                or any(host == domain or host.endswith("." + domain) for domain in excluded)):
            continue
        try:
            score = round(max(0.0, min(float(raw.get("score", 0)), 1.0)), 4)
        except (TypeError, ValueError, OverflowError):
            score = 0.0
        item = {"title": title, "url": url, "source": host, "snippet": snippet, "score": score}
        published = _safe_search_text(raw.get("published_date"))
        if published:
            item["published_date"] = published
        results.append(item)
    data = {
        "query": arguments["query"], "results": results, "returned": len(results),
        "total": None, "total_known": False, "source_result_count": len(payload["results"]),
        "truncated": len(payload["results"]) > len(selected), "filtered_results": len(selected) - len(results),
        "filters": {key: arguments[key] for key in _SEARCH_FILTERS if key in arguments},
        "max_results": arguments["max_results"],
    }
    if results:
        data.update(topic=arguments["topic"], elapsed_ms=max(0, elapsed_ms))
    result = _result(True, "ok" if results else "empty",
                     f"找到 {len(results)} 条网页结果" if results else f"没有找到与“{_safe_search_text(arguments['query'], 120)}”匹配的网页结果",
                     data=data)
    result["collected_at"] = agent_web_search_repo.current_agent_web_search_usage_date()
    return result



def _clean_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).replace("\r\n", "\n").replace("\r", "\n")
    text = _READ_CONTROL_RE.sub(" ", text)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(line.rstrip() for line in text.splitlines())).strip()


def _page_links(nodes: Iterable[Tag], base_url: str, page_url: str) -> list[dict[str, str]]:
    """保留去重链接，并在正文首次引用处保留目标地址；不发起请求。"""
    links: dict[str, dict[str, str]] = {}
    inlined: set[str] = set()
    for node in nodes:
        href = str(node.get("href", "")).strip()
        if not href or href.startswith("#"):
            continue
        try:
            url = normalize_public_url(urljoin(base_url, href))
        except (ValueError, TypeError):
            continue
        if url == page_url:
            continue
        text = node.get_text(" ", strip=True)
        label = text or str(node.get("aria-label", "")) or url
        if url not in links or links[url]["text"] == url:
            links[url] = {"text": redact_sensitive_text(_safe_search_text(label))[:200], "url": url}
        if node.name == "a" and url not in inlined and not node.find_parent(["pre", "code"]):
            if text != url:
                node.insert_after(NavigableString(f" ({url})"))
            inlined.add(url)
    return list(links.values())


def _is_json_type(content_type: str) -> bool:
    media_type = content_type.split(";", 1)[0].strip().lower()
    return media_type == "application/json" or (media_type.startswith("application/") and media_type.endswith("+json"))


def _redact_json(value: Any) -> Any:
    """按字段脱敏，避免文本替换破坏 JSON 语法；保留普通值和容器结构。"""
    if isinstance(value, dict):
        return {redact_sensitive_text(key): "********" if is_sensitive_key(unquote(unquote(key)))
                else _redact_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_json(item) for item in value]
    return redact_sensitive_text(value) if isinstance(value, str) else value


def _page_text(response: HttpResponse) -> tuple[str, str, list[dict[str, str]]]:
    """保留正文段落、表格单元和代码；不执行脚本，也不把二进制下载当网页。"""
    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    charset = re.search(r"charset\s*=\s*['\"]?([^;\s'\"]+)", response.headers.get("content-type", ""), re.IGNORECASE)
    encoding = charset.group(1) if charset else None
    is_json = _is_json_type(content_type)
    if content_type in _TEXT_TYPES or is_json:
        try:
            text = response.body.decode(encoding or ("utf-8-sig" if is_json else "utf-8"))
            if is_json:
                text = text.removeprefix("\ufeff")
                json.loads(text)
        except (UnicodeError, LookupError, json.JSONDecodeError):
            return "", "", []
        return ("", "", []) if "\x00" in text else ("", text.strip() if is_json else _clean_text(text), [])
    # 少数站点漏发 Content-Type；只接受可识别的 HTML，不猜测下载文件类型。
    if content_type not in _HTML_TYPES and (content_type or not re.match(
        rb"\s*<(?:!doctype\s+html|html|head|body|main|article)\b", response.body, re.IGNORECASE,
    )):
        return "", "", []
    if encoding:
        try:
            response.body.decode(encoding)
        except (UnicodeError, LookupError):
            encoding = None  # 错误响应头不能覆盖页面自身的有效 meta charset。
    soup = BeautifulSoup(response.body, "lxml", from_encoding=encoding)
    try:
        if soup.select_one("#challenge-form, #challenge-running, #cf-challenge-running"):
            return "", "", []
        page_title = soup.title.get_text(" ", strip=True) if soup.title else ""
        base_node = soup.find("base", href=True)
        base_url = response.url
        if base_node:
            try:
                base_url = urljoin(response.url, str(base_node["href"]))
            except ValueError:
                pass  # 无效 base 不应使有效正文整体不可读。
        for node in soup.select(
            "title, meta, script, style, noscript, iframe, object, embed, form, svg, canvas, template, "
            "[hidden], [aria-hidden='true']"
        ):
            node.decompose()
        pagination = soup.select(
            'a[href][rel~="next" i], a[href][rel~="prev" i], '
            'link[href][rel~="next" i], link[href][rel~="prev" i]'
        )
        # 移出导航但保留显式分页节点，供正文链接共用校验与去重；不恢复菜单正文。
        for node in soup.select("link, nav, aside, [role='navigation'], [role='banner'], [role='contentinfo']"):
            node.extract()
        # 文章内的 header/footer 可能包含标题、作者和日期，不应随站点导航删除。
        for node in soup.select("header, footer"):
            if not node.find_parent(["main", "article"]):
                node.extract()
        roots = (soup.select_one("main, [role='main']"), soup.find("article"), soup.body, soup)
        root = next((node for node in roots if node is not None
                     and (text := node.get_text(" ", strip=True)) and not _LOADING_TEXT_RE.fullmatch(text)), None)
        if root is None:
            return "", "", []
        title_node = root.find("h1")
        title = title_node.get_text(" ", strip=True) if title_node else page_title
        links = _page_links(chain(root.select("a[href]"), pagination), base_url, response.url)
        for value in list(root.strings):
            text = cast(NavigableString, value)
            if not text.find_parent("pre"):
                text.replace_with(re.sub(r"\s+", " ", str(text)))
        for node in root.find_all(["p", "div", "section", "article", "h1", "h2", "h3", "h4", "h5", "h6", "li", "tr", "pre", "blockquote"]):
            node.insert_before("\n")
            node.insert_after("\n")
        for node in root.find_all(["th", "td"]):
            node.insert_after("\t")
        for node in root.find_all("br"):
            node.replace_with("\n")
        return _clean_text(title), _clean_text(root.get_text()), links
    finally:
        soup.decompose()


def _derive_title(content: str, host: str) -> str:
    for line in content.splitlines()[:30]:
        candidate = _MARKDOWN_PREFIX_RE.sub("", line).strip()
        candidate = " ".join(candidate.split())
        if 3 <= len(candidate) <= 180 and not candidate.startswith(("http://", "https://")):
            return redact_sensitive_text(candidate)[:180]
    return host[:180]


def _map_page_response(response: HttpResponse, arguments: dict[str, Any], elapsed_ms: int) -> dict[str, Any]:
    title, content, links = _page_text(response)
    if not content:
        return _result(False, "unreadable", "未取得可读正文；本地读取仅支持公开 HTML/文本/JSON，不执行 JavaScript 或读取登录、验证页面及二进制文件")
    result_url = response.url
    host = (httpx.URL(result_url).host or "").casefold()
    original_chars = len(content)
    safe_content = (json.dumps(_redact_json(json.loads(content)), ensure_ascii=False, separators=(",", ":"))
                    if _is_json_type(response.headers.get("content-type", "")) else redact_sensitive_text(content))
    limited = safe_content[:arguments["max_chars"]]
    title = redact_sensitive_text(title)[:180] if title else _derive_title(limited, host)
    result = _result(True, "ok", f"已读取 {host} 的公开网页正文", data={
        "requested_url": arguments["url"], "url": result_url, "source": host, "title": title,
        "content": limited, "content_chars": len(limited), "original_content_chars": original_chars,
        "truncated": len(safe_content) > arguments["max_chars"], "provider": "local", "cached": False,
        "elapsed_ms": max(0, elapsed_ms), "credits_used": 0,
        "links": links, "links_total": len(links),
    })
    result["collected_at"] = agent_web_search_repo.current_agent_web_search_usage_date()
    return result



async def _search_tavily(
    arguments: dict[str, Any],
    *,
    api_key: str,
    depth: str,
    client_factory: Callable[..., FixedHostHttpClient] = FixedHostHttpClient,
) -> dict[str, Any]:
    client = client_factory(
        allowed_hosts={"api.tavily.com"},
        timeout_seconds=_int_config(
            "TAVILY_TIMEOUT_SECONDS", 10, minimum=2, maximum=30
        ),
        max_response_bytes=512 * 1024,
        max_redirects=0,
        user_agent="MediaFlux-Agent/1.0",
        pin_resolved_address=True,
    )
    body: dict[str, Any] = {
        **arguments,
        "search_depth": depth,
        "include_published_date": True,
        "include_answer": False,
        "include_raw_content": False,
        "include_images": False,
    }
    started = time.monotonic()
    try:
        response = await client.post_json(
            _TAVILY_ENDPOINT,
            json=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            max_redirects=0,
        )
        elapsed_ms = round((time.monotonic() - started) * 1000)
        if not 200 <= response.status_code < 300:
            return _provider_error(response.status_code)
        try:
            payload = json.loads(response.text)
        except (TypeError, ValueError):
            return _result(
                False, "invalid_response", "网页搜索服务返回了无法识别的数据"
            )
        return _map_search_response(payload, arguments, elapsed_ms)
    except httpx.TimeoutException:
        return _result(False, "timeout", "网页搜索服务响应超时")
    except (HttpSecurityError, HttpResponseTooLarge, HttpInvalidResponse):
        return _result(False, "invalid_response", "网页搜索响应未通过安全校验")
    except (httpx.HTTPError, HttpError):
        return _result(False, "unavailable", "网页搜索服务暂时不可用")
    except Exception as exc:
        logger.warning("Tavily 搜索失败 type=%s", type(exc).__name__)
        return _result(False, "unavailable", "网页搜索服务暂时不可用")
    finally:
        await client.aclose()


async def _read_local(
    arguments: dict[str, Any],
    *,
    client_factory: Callable[..., FixedHostHttpClient] = FixedHostHttpClient,
) -> dict[str, Any]:
    timeout = _int_config("WEB_READ_TIMEOUT_SECONDS", 10, minimum=2, maximum=30)
    started = time.monotonic()
    current = arguments["url"]
    visited: set[str] = set()
    try:
        # 整次读取（DNS、所有跳转和响应体）共用预算，不为每次跳转重新计时。
        async with asyncio.timeout(timeout):
            for _ in range(_MAX_REDIRECTS + 1):
                current = normalize_public_url(current)
                if current in visited:
                    return _result(False, "unreadable", "目标网页出现循环重定向")
                visited.add(current)
                client = client_factory(
                    allowed_hosts={httpx.URL(current).host},
                    timeout_seconds=timeout,
                    get_timeout_retries=0,
                    max_response_bytes=_MAX_RESPONSE_BYTES,
                    max_redirects=0,
                    user_agent="MediaFlux-Agent/1.0",
                    pin_resolved_address=True,
                )
                try:
                    response = await client.get(current, follow_redirects=False, headers={
                        "Accept": "text/html,application/xhtml+xml,application/json,text/plain,text/markdown;q=0.9",
                    })
                finally:
                    await client.aclose()
                if response.status_code in _REDIRECT_CODES:
                    location = response.headers.get("location", "").strip()
                    if not location:
                        return _result(False, "invalid_response", "目标网页重定向缺少地址")
                    current = str(httpx.URL(current).join(location))
                    continue
                if not 200 <= response.status_code < 300:
                    return _http_error(response.status_code)
                return _map_page_response(response, arguments, round((time.monotonic() - started) * 1000))
        return _result(False, "unreadable", "目标网页重定向次数过多")
    except (httpx.TimeoutException, TimeoutError):
        return _result(False, "timeout", "目标网页读取超时")
    except HttpResponseTooLarge:
        return _result(False, "invalid_response", "目标网页超过 2 MiB 读取上限")
    except (WebEvidenceError, HttpSecurityError, HttpInvalidResponse, httpx.InvalidURL):
        return _result(False, "invalid_response", "目标网页或重定向地址未通过公开网页校验")
    except (httpx.HTTPError, HttpError):
        return _result(False, "unavailable", "目标网站暂时不可用")
    except Exception as exc:  # noqa: BLE001 - 外部网页最终故障边界，不回显凭据或响应体
        logger.warning("本地网页读取失败 type=%s", type(exc).__name__)
        return _result(False, "unavailable", "目标网页读取失败")


def search_web(
    query: str, *, max_results: int = 5, topic: str = "general", time_range: str | None = None,
    include_domains: list[str] | None = None, exclude_domains: list[str] | None = None,
    start_date: str | None = None, end_date: str | None = None,
) -> dict[str, Any]:
    """读取公开网页搜索结果；returned 仅为返回条数，不代表互联网匹配总数。"""
    normalized = normalize_search_arguments({
        "query": query, "max_results": max_results, "topic": topic, "time_range": time_range,
        "include_domains": include_domains, "exclude_domains": exclude_domains,
        "start_date": start_date, "end_date": end_date,
    })
    provider_cap = _int_config("TAVILY_MAX_RESULTS", 5, minimum=1, maximum=10)
    normalized["max_results"] = min(normalized["max_results"], provider_cap)
    if not _search_enabled():
        return _result(
            False,
            "disabled",
            "通用网页搜索尚未启用",
        )
    api_key = (get("TAVILY_API_KEY", "") or "").strip()
    if not api_key:
        return _result(
            False,
            "configuration_missing",
            "通用网页搜索缺少 Tavily API Key",
        )
    depth = (get("TAVILY_SEARCH_DEPTH", "basic") or "basic").strip().lower()
    if depth not in {"basic", "advanced"}:
        depth = "basic"
    key = _search_cache_key(normalized, depth)
    cached = _cached_result(key)
    if cached is not None:
        return cached

    try:
        ensure_sync_bridge_available()
    except AsyncBridgeUnavailable:
        return _result(
            False,
            "unavailable",
            "网页搜索当前调用上下文不可用",
            error="请从同步 Agent 查询入口调用网页搜索。",
        )

    def compute() -> dict[str, Any]:
        cost = 2 if depth == "advanced" else 1
        usage_date = agent_web_search_repo.current_agent_web_search_usage_date()
        charged = False
        try:
            daily_limit = _int_config(
                "TAVILY_DAILY_CREDIT_LIMIT", 100, minimum=1, maximum=100000
            )
            if not agent_web_search_repo.reserve_agent_web_search_credits(
                provider="tavily",
                usage_date=usage_date,
                cost=cost,
                daily_limit=daily_limit,
            ):
                return _result(
                    False,
                    "budget_exhausted",
                    "今日网页搜索额度已用完",
                    data={"daily_limit": daily_limit, "usage_date": usage_date},
                )
            charged = True
            result = run_awaitable_sync(
                _search_tavily(normalized, api_key=api_key, depth=depth)
            )
            if result["ok"]:
                result["data"] = dict(result["data"])
                result["data"].update(
                    {
                        "provider": "tavily",
                        "search_depth": depth,
                        "credits_used": cost,
                        "cached": False,
                    }
                )
                charged = False
            return result
        finally:
            if charged:
                agent_web_search_repo.refund_agent_web_search_credits(
                    provider="tavily", usage_date=usage_date, cost=cost
                )

    def write_cache(result: dict[str, Any]) -> None:
        if not result["ok"]:
            return
        try:
            _store_cached_result(key, result, ttl_key="TAVILY_CACHE_TTL_SECONDS")
        except Exception as exc:  # noqa: BLE001 - 缓存失败不得覆盖有效结果
            logger.warning("Tavily 网页搜索缓存失败 type=%s", type(exc).__name__)

    return web_request_singleflight.run_cached(
        ("search", key),
        read_cache=lambda: _cached_result(key),
        compute=compute,
        write_cache=write_cache,
        unavailable=lambda: _result(False, "unavailable", "网页搜索服务暂时不可用"),
        timeout=WEB_REQUEST_SINGLE_FLIGHT_WAIT_SECONDS,
    )


def read_web(url: str, *, max_chars: int = 10_000) -> dict[str, Any]:
    """读取预算内完整正文；content 不做展示截断，truncated 明确标记长度边界。"""
    normalized = normalize_read_arguments({"url": url, "max_chars": max_chars})
    if not _read_enabled():
        return _result(
            False,
            "disabled",
            "通用网页读取尚未启用",
        )
    key = _read_cache_key(normalized)
    cached = _cached_result(key)
    if cached is not None:
        return cached
    try:
        ensure_sync_bridge_available()
    except AsyncBridgeUnavailable:
        return _result(
            False,
            "unavailable",
            "网页读取当前调用上下文不可用",
            error="请从同步 Agent 查询入口调用网页读取。",
        )

    def write_cache(result: dict[str, Any]) -> None:
        if not result["ok"]:
            return
        try:
            _store_cached_result(key, result, ttl_key="WEB_READ_CACHE_TTL_SECONDS")
        except Exception as exc:  # noqa: BLE001 - 缓存失败不得覆盖有效结果
            logger.warning("本地网页读取缓存失败 type=%s", type(exc).__name__)

    return web_request_singleflight.run_cached(
        ("read", key),
        read_cache=lambda: _cached_result(key),
        compute=lambda: run_awaitable_sync(_read_local(normalized)),
        write_cache=write_cache,
        unavailable=lambda: _result(False, "unavailable", "网页读取服务暂时不可用"),
        timeout=WEB_REQUEST_SINGLE_FLIGHT_WAIT_SECONDS,
    )
