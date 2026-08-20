"""自动整理专用的受控 Tavily 标题线索。

本模块不返回网页 URL、摘要或答案，也不能直接决定归档目标。调用方只能把
返回的标题重新交给 TMDB 严格匹配与季集校验。
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import unicodedata
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable

import httpx

from app.agent.async_bridge import (
    AsyncBridgeUnavailable,
    ensure_sync_bridge_available,
    run_awaitable_sync,
)
from app.config import get, get_bool
from app.logger import get_logger
from app.sensitive_data import contains_sensitive_credential

logger = get_logger(__name__)

_TAVILY_ENDPOINT = "https://api.tavily.com/search"
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
# Tavily 常把站点名拼在结果标题末尾。只剥离白名单站点后缀并同时保留
# 原标题，避免把作品自身的副标题误删；归档仍须通过既有 TMDB strict 同 ID。
_KNOWN_RESULT_SITE_SUFFIX = re.compile(
    r"(?ix)\s*(?:[-|·—–]\s*)"
    r"(?:imdb|wikipedia|myanimelist(?:\.net)?|anilist|bangumi|"
    r"the\s+movie\s+database(?:\s*\(tmdb\))?|tmdb|豆瓣)\s*$"
)
_CACHE_LIMIT = 128
_NEGATIVE_CACHE_TTL_SECONDS = 30.0
_SINGLE_FLIGHT_WAIT_SECONDS = 35.0
_cache: dict[str, tuple[float, "RecognitionWebHintResult"]] = {}
_inflight: dict[str, threading.Event] = {}
_cache_lock = threading.RLock()
_generation = 0


@dataclass(frozen=True, slots=True)
class RecognitionWebHintResult:
    titles: tuple[str, ...] = ()
    attempted: bool = False
    cached: bool = False
    status: str = "disabled"
    error: str = ""


def _bounded_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(str(get(name, str(default)) or default).strip())
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


def _safe_text(value: object, maximum: int) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    return " ".join(_CONTROL_RE.sub(" ", text).split())[:maximum]


def _normalize_title(value: object) -> str:
    title = _safe_text(value, 200).strip()
    if not title or contains_sensitive_credential(title):
        return ""
    return title


def _result_title_variants(value: object) -> tuple[str, ...]:
    """生成安全的网页标题变体；仅去掉已知站点尾缀。"""
    title = _normalize_title(value)
    if not title:
        return ()
    stripped = _KNOWN_RESULT_SITE_SUFFIX.sub("", title).strip(" ._-|·—–")
    if (
        not stripped
        or len(stripped) < 3
        or stripped.casefold() == title.casefold()
        or contains_sensitive_credential(stripped)
    ):
        return (title,)
    return (title, stripped)


def _cache_key(title: str, media_type: str, year: str) -> str:
    raw = f"{title.casefold()}\0{media_type}\0{year}"
    return hashlib.sha256(raw.encode("utf-8", "ignore")).hexdigest()


def _cached(key: str) -> RecognitionWebHintResult | None:
    current = time.monotonic()
    with _cache_lock:
        entry = _cache.get(key)
        if entry is None:
            return None
        if current >= entry[0]:
            _cache.pop(key, None)
            return None
        value = entry[1]
    return RecognitionWebHintResult(
        titles=value.titles,
        attempted=value.attempted,
        cached=True,
        status=value.status,
        error=value.error,
    )


def _store(
    key: str,
    value: RecognitionWebHintResult,
    *,
    ttl_seconds: float | None = None,
    generation: int | None = None,
) -> bool:
    if ttl_seconds is None:
        ttl_seconds = float(
            _bounded_int("TAVILY_CACHE_TTL_SECONDS", 900, minimum=30, maximum=86400)
        )
    expires_at = time.monotonic() + max(1.0, float(ttl_seconds))
    with _cache_lock:
        if generation is not None and generation != _generation:
            return False
        if key not in _cache and len(_cache) >= _CACHE_LIMIT:
            oldest = min(_cache, key=lambda item: _cache[item][0])
            _cache.pop(oldest, None)
        _cache[key] = (expires_at, value)
        return True


def _reserve_daily(limit: int) -> bool:
    from app.database import reserve_agent_web_search_credits

    return reserve_agent_web_search_credits(
        provider="tavily_recognition",
        usage_date=date.today().isoformat(),
        cost=1,
        daily_limit=limit,
    )


async def _request_titles(
    query: str,
    *,
    api_key: str,
    client_factory: Callable[..., Any] | None = None,
) -> RecognitionWebHintResult:
    # 延迟导入，避免 indexers 包初始化反向导入 scraper。
    if client_factory is None:
        from app.indexers.http import FixedHostHttpClient

        client_factory = FixedHostHttpClient
    client = client_factory(
        allowed_hosts={"api.tavily.com"},
        timeout_seconds=_bounded_int(
            "TAVILY_TIMEOUT_SECONDS", 10, minimum=2, maximum=30
        ),
        max_response_bytes=128 * 1024,
        max_redirects=0,
        user_agent="MediaFlux-RecognitionHints/1.0",
        pin_resolved_address=True,
    )
    try:
        response = await client.post_json(
            _TAVILY_ENDPOINT,
            json={
                "query": query,
                "topic": "general",
                "search_depth": "basic",
                "max_results": 5,
                "include_answer": False,
                "include_raw_content": False,
                "include_images": False,
            },
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            max_redirects=0,
        )
        if not 200 <= int(response.status_code) < 300:
            return RecognitionWebHintResult(
                attempted=True,
                status="provider_error",
                error=f"Tavily 返回 HTTP {int(response.status_code)}",
            )
        try:
            payload = json.loads(response.text)
        except (TypeError, ValueError):
            return RecognitionWebHintResult(
                attempted=True,
                status="invalid_response",
                error="Tavily 返回的数据无法解析",
            )
        rows = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            rows = []
        titles: list[str] = []
        seen: set[str] = set()
        for row in rows[:5]:
            if not isinstance(row, dict):
                continue
            for title in _result_title_variants(row.get("title")):
                marker = title.casefold()
                if marker not in seen:
                    seen.add(marker)
                    titles.append(title)
        return RecognitionWebHintResult(
            titles=tuple(titles),
            attempted=True,
            status="ok" if titles else "no_result",
        )
    except (httpx.TimeoutException, httpx.RequestError):
        return RecognitionWebHintResult(
            attempted=True, status="unavailable", error="Tavily 请求超时或连接失败"
        )
    except Exception as exc:
        # 安全 HTTP 客户端错误不向业务层暴露地址、DNS 或凭据细节。
        logger.info("整理标题线索请求失败 type=%s", type(exc).__name__)
        return RecognitionWebHintResult(
            attempted=True, status="unavailable", error="Tavily 暂时不可用"
        )
    finally:
        await client.aclose()


def _close_awaitable(value: object) -> None:
    closer = getattr(value, "close", None)
    if callable(closer):
        closer()


def search_recognition_titles(
    title: str,
    *,
    media_type: str = "",
    year: str = "",
    reserve_daily: Callable[[int], bool] = _reserve_daily,
    runner: Callable[[Any], RecognitionWebHintResult] = run_awaitable_sync,
    client_factory: Callable[..., Any] | None = None,
) -> RecognitionWebHintResult:
    """取得标题线索；默认关闭，且同查询一次最多消耗 1 个独立额度。"""
    if not get_bool("ORGANIZE_TAVILY_HINTS_ENABLED", False):
        return RecognitionWebHintResult(status="disabled")
    normalized_title = _normalize_title(title)
    if not normalized_title:
        return RecognitionWebHintResult(status="unsafe_input")
    normalized_type = str(media_type or "").strip().lower()
    if normalized_type not in {"movie", "tv"}:
        normalized_type = ""
    normalized_year = str(year or "").strip()
    if normalized_year and not re.fullmatch(r"(?:18|19|20|21)\d{2}", normalized_year):
        normalized_year = ""
    api_key = str(get("TAVILY_API_KEY", "") or "").strip()
    if not api_key:
        return RecognitionWebHintResult(status="misconfigured")

    key = _cache_key(normalized_title, normalized_type, normalized_year)
    cached = _cached(key)
    if cached is not None:
        return cached
    # 默认同步桥若处于活动事件循环线程，会在真正创建协程与扣减额度前
    # 快速失败；注入 runner 的测试/异步适配缝不受影响。
    if runner is run_awaitable_sync:
        try:
            ensure_sync_bridge_available()
        except AsyncBridgeUnavailable:
            return RecognitionWebHintResult(status="unavailable")

    with _cache_lock:
        generation = _generation
        in_flight = _inflight.get(key)
        if in_flight is None:
            in_flight = threading.Event()
            _inflight[key] = in_flight
            owner = True
        else:
            owner = False
    if not owner:
        if not in_flight.wait(timeout=_SINGLE_FLIGHT_WAIT_SECONDS):
            return RecognitionWebHintResult(status="unavailable")
        cached = _cached(key)
        return cached or RecognitionWebHintResult(status="unavailable")

    owner_event = in_flight
    result = RecognitionWebHintResult(status="unavailable")
    awaitable: object | None = None
    try:
        daily_limit = _bounded_int(
            "ORGANIZE_TAVILY_HINTS_DAILY_CREDIT_LIMIT",
            20,
            minimum=1,
            maximum=100_000,
        )
        if not reserve_daily(daily_limit):
            result = RecognitionWebHintResult(status="budget_exhausted")
        else:
            query_parts = [f'"{normalized_title}"']
            if normalized_year:
                query_parts.append(normalized_year)
            if normalized_type == "tv":
                query_parts.append("TV series")
            elif normalized_type == "movie":
                query_parts.append("movie")
            awaitable = _request_titles(
                " ".join(query_parts),
                api_key=api_key,
                client_factory=client_factory,
            )
            result = runner(awaitable)
            awaitable = None

    except AsyncBridgeUnavailable:
        _close_awaitable(awaitable)
        result = RecognitionWebHintResult(status="unavailable")
    except Exception as exc:
        _close_awaitable(awaitable)
        logger.info("整理标题线索执行失败 type=%s", type(exc).__name__)
        result = RecognitionWebHintResult(
            attempted=True, status="unavailable", error="Tavily 暂时不可用"
        )
    finally:
        # 只有当前 owner 能释放自己登记的 single-flight 事件。配置热更新
        # 后新一代同 key 请求不得被旧请求误删或提前唤醒。
        with _cache_lock:
            if _inflight.get(key) is owner_event:
                _inflight.pop(key, None)
                owner_event.set()

    if result.status in {"ok", "no_result"}:
        _store(key, result, generation=generation)
    elif result.status in {
        "provider_error",
        "invalid_response",
        "unavailable",
        "budget_exhausted",
    }:
        _store(
            key,
            result,
            ttl_seconds=_NEGATIVE_CACHE_TTL_SECONDS,
            generation=generation,
        )
    return result


def clear_recognition_web_hint_cache() -> None:
    """配置热更新后清空标题线索缓存，并释放等待中的相同查询。"""
    global _generation
    with _cache_lock:
        _generation += 1
        _cache.clear()
        pending = tuple(_inflight.values())
        _inflight.clear()
    for event in pending:
        event.set()


def reset_recognition_web_hints_for_tests() -> None:
    clear_recognition_web_hint_cache()
