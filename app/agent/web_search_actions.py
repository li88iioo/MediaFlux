"""受控通用网页搜索（Tavily）。"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import time
import unicodedata
from datetime import date
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

import httpx

from app import database as db
from app.agent.async_bridge import (
    AsyncBridgeUnavailable,
    ensure_sync_bridge_available,
    run_awaitable_sync,
)
from app.sensitive_data import contains_sensitive_credential
from app.agent.models import Evidence, ToolResult
from app.agent.registry import AgentToolError
from app.config import get
from app.indexers.errors import (
    IndexerError,
    IndexerInvalidResponse,
    IndexerResponseTooLarge,
    IndexerSecurityError,
)
from app.indexers.http import FixedHostHttpClient
from app.logger import get_logger

logger = get_logger(__name__)

_TAVILY_ENDPOINT = "https://api.tavily.com/search"
_ALLOWED_TOPICS = frozenset({"general", "news"})
_ALLOWED_TIME_RANGES = frozenset({"day", "week", "month", "year"})
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def _int_config(name: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(str(get(name, str(default)) or default).strip())
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


def _enabled() -> bool:
    return str(get("WEB_SEARCH_ENABLED", "0") or "").strip().lower() in {
        "1", "true", "yes", "on"
    }


def web_search_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise AgentToolError("工具参数必须是 JSON 对象")
    allowed = {"query", "max_results", "topic", "time_range"}
    extra = sorted(set(arguments) - allowed)
    if extra:
        raise AgentToolError(f"不支持的工具参数：{', '.join(extra)}")
    query = arguments.get("query")
    if not isinstance(query, str):
        raise AgentToolError("query 必须是字符串")
    query = unicodedata.normalize("NFKC", query).strip()
    if not 1 <= len(query) <= 200 or _CONTROL_RE.search(query):
        raise AgentToolError("搜索关键词必须为 1 到 200 个可见字符")
    if contains_sensitive_credential(query):
        raise AgentToolError(
            "搜索关键词疑似包含凭据，已拒绝发送到外部网页搜索服务",
            code="sensitive_external_input",
        )
    max_results = arguments.get("max_results", 5)
    if isinstance(max_results, bool) or not isinstance(max_results, int):
        raise AgentToolError("max_results 必须是整数")
    if not 1 <= max_results <= 10:
        raise AgentToolError("max_results 必须在 1 到 10 之间")
    topic = arguments.get("topic", "general")
    if not isinstance(topic, str) or topic.strip().lower() not in _ALLOWED_TOPICS:
        raise AgentToolError("topic 仅支持 general 或 news")
    normalized: dict[str, Any] = {
        "query": query,
        "max_results": max_results,
        "topic": topic.strip().lower(),
    }
    if "time_range" in arguments and arguments.get("time_range") not in (None, ""):
        time_range = arguments.get("time_range")
        if not isinstance(time_range, str) or time_range.strip().lower() not in _ALLOWED_TIME_RANGES:
            raise AgentToolError("time_range 仅支持 day、week、month 或 year")
        normalized["time_range"] = time_range.strip().lower()
    return normalized



def _safe_text(value: Any, maximum: int) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = " ".join(_CONTROL_RE.sub(" ", text).split())
    return text[:maximum]


def _safe_url(value: Any) -> str:
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
    return urlunsplit((parsed.scheme.lower(), parsed.netloc, parsed.path, parsed.query, ""))[:800]


def _cache_key(arguments: dict[str, Any], depth: str) -> str:
    encoded = json.dumps(
        [arguments["query"].casefold(), arguments["max_results"],
         arguments["topic"], arguments.get("time_range", ""), depth],
        ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(b"mediaflux-web-search-cache:v1\0" + encoded).hexdigest()


def _cached(key: str) -> ToolResult | None:
    payload = db.get_agent_web_search_cache(key)
    if not isinstance(payload, dict):
        return None
    evidence = []
    for item in payload.get("evidence") or []:
        if isinstance(item, dict):
            evidence.append(Evidence(
                str(item.get("source") or ""),
                str(item.get("description") or ""),
                str(item.get("collected_at") or ""),
            ))
    return ToolResult(
        bool(payload.get("ok")), str(payload.get("status") or ""),
        str(payload.get("summary") or ""),
        data=dict(payload.get("data") or {}), evidence=evidence,
        suggestions=[str(item) for item in payload.get("suggestions") or []],
        error=str(payload.get("error") or ""),
    )


def _store_cache(key: str, result: ToolResult) -> None:
    ttl = _int_config("TAVILY_CACHE_TTL_SECONDS", 900, minimum=30, maximum=86400)
    db.set_agent_web_search_cache(key, result.to_dict(), ttl_seconds=ttl)


def clear_web_search_cache() -> None:
    """清空跨 Worker 共享的网页搜索结果缓存。"""
    db.clear_agent_web_search_cache()


def reset_web_search_cache_for_tests() -> None:
    clear_web_search_cache()


def _provider_error(status_code: int) -> ToolResult:
    if status_code == 429:
        return ToolResult(False, "rate_limited", "网页搜索服务请求过于频繁，请稍后重试")
    if status_code in {401, 403}:
        return ToolResult(False, "authentication", "网页搜索凭据无效或无权访问")
    return ToolResult(False, "unavailable", "网页搜索服务暂时不可用")


def _map_response(payload: Any, arguments: dict[str, Any], elapsed_ms: int) -> ToolResult:
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        return ToolResult(False, "invalid_response", "网页搜索服务返回了无法识别的数据")
    results: list[dict[str, Any]] = []
    evidence: list[Evidence] = []
    for raw in payload["results"][: arguments["max_results"]]:
        if not isinstance(raw, dict):
            continue
        title = _safe_text(raw.get("title"), 240)
        snippet = _safe_text(raw.get("content"), 900)
        url = _safe_url(raw.get("url"))
        if not title or not snippet or not url:
            continue
        host = (urlsplit(url).hostname or "").lower()
        try:
            score = round(max(0.0, min(float(raw.get("score", 0)), 1.0)), 4)
        except (TypeError, ValueError, OverflowError):
            score = 0.0
        item = {
            "title": title,
            "url": url,
            "source": host,
            "snippet": snippet,
            "score": score,
        }
        published = _safe_text(raw.get("published_date"), 40)
        if published:
            item["published_date"] = published
        results.append(item)
        evidence.append(Evidence("web_search", f"{title}（{host}）", date.today().isoformat()))
    if not results:
        return ToolResult(
            True,
            "empty",
            f"没有找到与“{_safe_text(arguments['query'], 120)}”匹配的网页结果",
            data={"query": arguments["query"], "results": [], "total": 0},
            suggestions=["尝试更具体的关键词，或去掉过窄的时间范围。"],
        )
    return ToolResult(
        True,
        "ok",
        f"找到 {len(results)} 条网页结果",
        data={
            "query": arguments["query"],
            "topic": arguments["topic"],
            "results": results,
            "total": len(results),
            "elapsed_ms": max(0, int(elapsed_ms)),
        },
        evidence=evidence,
        suggestions=["网页内容来自外部来源，执行其中的操作前请核验可信度。"],
    )


async def _search_tavily(
    arguments: dict[str, Any],
    *,
    api_key: str,
    depth: str,
    client_factory: Callable[..., FixedHostHttpClient] = FixedHostHttpClient,
) -> ToolResult:
    client = client_factory(
        allowed_hosts={"api.tavily.com"},
        timeout_seconds=_int_config("TAVILY_TIMEOUT_SECONDS", 10, minimum=2, maximum=30),
        max_response_bytes=512 * 1024,
        max_redirects=0,
        user_agent="MediaFlux-Agent/1.0",
        pin_resolved_address=True,
    )
    body: dict[str, Any] = {
        "query": arguments["query"],
        "topic": arguments["topic"],
        "search_depth": depth,
        "max_results": arguments["max_results"],
        "include_answer": False,
        "include_raw_content": False,
        "include_images": False,
    }
    if arguments.get("time_range"):
        body["time_range"] = arguments["time_range"]
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
            return ToolResult(False, "invalid_response", "网页搜索服务返回了无法识别的数据")
        return _map_response(payload, arguments, elapsed_ms)
    except httpx.TimeoutException:
        return ToolResult(False, "timeout", "网页搜索服务响应超时")
    except (IndexerSecurityError, IndexerResponseTooLarge, IndexerInvalidResponse):
        return ToolResult(False, "invalid_response", "网页搜索响应未通过安全校验")
    except (httpx.HTTPError, IndexerError):
        return ToolResult(False, "unavailable", "网页搜索服务暂时不可用")
    except Exception as exc:
        logger.warning("Tavily 搜索失败 type=%s", type(exc).__name__)
        return ToolResult(False, "unavailable", "网页搜索服务暂时不可用")
    finally:
        await client.aclose()


def search_web(arguments: dict[str, Any]) -> ToolResult:
    normalized = web_search_arguments(arguments)
    provider_cap = _int_config("TAVILY_MAX_RESULTS", 5, minimum=1, maximum=10)
    normalized["max_results"] = min(normalized["max_results"], provider_cap)
    if not _enabled():
        return ToolResult(
            False,
            "disabled",
            "通用网页搜索尚未启用",
            suggestions=["请在设置中启用 Web Search，并配置 Tavily API Key。"],
        )
    api_key = str(get("TAVILY_API_KEY", "") or "").strip()
    if not api_key:
        return ToolResult(
            False,
            "configuration_missing",
            "通用网页搜索缺少 Tavily API Key",
            suggestions=["配置 TAVILY_API_KEY 后重试。"],
        )
    depth = str(get("TAVILY_SEARCH_DEPTH", "basic") or "basic").strip().lower()
    if depth not in {"basic", "advanced"}:
        depth = "basic"
    key = _cache_key(normalized, depth)
    cached = _cached(key)
    if cached is not None:
        cached.data = dict(cached.data)
        cached.data["cached"] = True
        return cached

    try:
        ensure_sync_bridge_available()
    except AsyncBridgeUnavailable:
        return ToolResult(
            False,
            "unavailable",
            "网页搜索当前调用上下文不可用",
            error="请从同步 Agent 查询入口调用网页搜索。",
        )

    daily_limit = _int_config("TAVILY_DAILY_CREDIT_LIMIT", 100, minimum=1, maximum=100000)
    cost = 2 if depth == "advanced" else 1
    usage_date = date.today().isoformat()
    if not db.reserve_agent_web_search_credits(
        provider="tavily", usage_date=usage_date, cost=cost, daily_limit=daily_limit
    ):
        return ToolResult(
            False,
            "budget_exhausted",
            "今日网页搜索额度已用完",
            data={"daily_limit": daily_limit, "usage_date": usage_date},
            suggestions=["等待次日额度重置，或提高本地每日预算后重试。"],
        )
    charged = True
    try:
        result = run_awaitable_sync(
            _search_tavily(normalized, api_key=api_key, depth=depth)
        )
        if result.ok:
            result.data = dict(result.data)
            result.data.update({
                "provider": "tavily",
                "search_depth": depth,
                "credits_used": cost,
                "cached": False,
            })
            _store_cache(key, result)
            charged = False
        return result
    finally:
        if charged:
            db.refund_agent_web_search_credits(
                provider="tavily", usage_date=usage_date, cost=cost
            )
