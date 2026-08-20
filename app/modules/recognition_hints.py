"""自动整理使用的豆瓣/Bangumi 标题线索。

外部数据只用于生成第二轮 TMDB 查询词，不提供 TMDB ID，也不绕过现有严格评分。
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass

from app.config import get_bool
from app.discovery.models import MediaCard
from app.discovery.search import get_discovery_search_service
from app.logger import get_logger

logger = get_logger(__name__)
_CACHE_TTL_SECONDS = 600.0
_CACHE_LIMIT = 256
_AUTO_TIMEOUT_SECONDS = 4.0


@dataclass(frozen=True)
class RecognitionHintResult:
    items: tuple[MediaCard, ...]
    providers: tuple[str, ...]
    errors: tuple[dict, ...] = ()
    cached: bool = False


_lock = threading.Lock()
_cache: OrderedDict[tuple[str, str, tuple[str, ...]], tuple[float, RecognitionHintResult]] = OrderedDict()


def enabled_hint_providers(media_type: str) -> tuple[str, ...]:
    providers: list[str] = []
    if (
        get_bool("ORGANIZE_DOUBAN_HINTS_ENABLED", False)
        and get_bool("DISCOVERY_DOUBAN_ENABLED", True)
    ):
        providers.append("douban")
    if str(media_type or "").lower() == "tv" and get_bool(
        "ORGANIZE_BANGUMI_HINTS_ENABLED", False
    ):
        providers.append("bangumi")
    return tuple(providers)


def search_recognition_hints(
    query: str,
    media_type: str,
    *,
    timeout_seconds: float = _AUTO_TIMEOUT_SECONDS,
) -> RecognitionHintResult:
    normalized = " ".join(str(query or "").split()).strip()
    providers = enabled_hint_providers(media_type)
    if not normalized or not providers:
        return RecognitionHintResult((), providers)
    key = (normalized.casefold(), str(media_type or "").lower(), providers)
    now = time.monotonic()
    with _lock:
        cached = _cache.get(key)
        if cached and cached[0] > now:
            _cache.move_to_end(key)
            value = cached[1]
            return RecognitionHintResult(value.items, value.providers, value.errors, True)
        if cached:
            _cache.pop(key, None)

    try:
        result = get_discovery_search_service().search(
            normalized,
            1,
            list(providers),
            timeout_seconds=max(0.5, min(float(timeout_seconds), 5.0)),
        )
        items = tuple(
            card for card in result.items
            if card.media_type in {str(media_type or "").lower(), "all"}
        )[:8]
        value = RecognitionHintResult(
            items,
            tuple(result.providers_attempted),
            tuple(dict(item) for item in result.errors),
        )
    except Exception as exc:
        logger.info(
            "自动识别外部线索不可用 query=%s type=%s error_type=%s",
            normalized[:80], media_type, type(exc).__name__,
        )
        value = RecognitionHintResult((), providers, ({"code": "unavailable"},))

    with _lock:
        _cache[key] = (now + _CACHE_TTL_SECONDS, value)
        _cache.move_to_end(key)
        while len(_cache) > _CACHE_LIMIT:
            _cache.popitem(last=False)
    return value


def clear_recognition_hint_cache() -> None:
    with _lock:
        _cache.clear()
