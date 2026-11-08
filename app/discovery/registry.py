"""探索 Provider 注册表、栏目定义与筛选白名单。"""
from __future__ import annotations

import re
import threading
from copy import deepcopy
from datetime import date
from typing import Any

from app import config
from app.discovery.models import ProviderNotConfigured, ProviderUnavailable
from app.logger import get_logger

logger = get_logger(__name__)

_SECTION_DEFINITIONS: tuple[dict[str, Any], ...] = (
    {"key": "tmdb-trending-week", "title": "本周流行", "provider": "tmdb", "category": "trending_week", "media_type": "all"},
    {"key": "tmdb-popular-movies", "title": "TMDB 热门电影", "provider": "tmdb", "category": "popular", "media_type": "movie"},
    {"key": "tmdb-popular-tv", "title": "TMDB 热门电视剧", "provider": "tmdb", "category": "popular", "media_type": "tv"},
    {"key": "douban-showing", "title": "正在热映", "provider": "douban", "category": "movie_showing", "media_type": "movie"},
    {"key": "douban-soon", "title": "即将上映", "provider": "douban", "category": "movie_soon", "media_type": "movie"},
    {"key": "douban-hot-movies", "title": "豆瓣热门电影", "provider": "douban", "category": "movie_hot", "media_type": "movie"},
    {"key": "douban-top250", "title": "豆瓣电影 TOP250", "provider": "douban", "category": "movie_top250", "media_type": "movie"},
    {"key": "douban-hot-tv", "title": "豆瓣热门电视剧", "provider": "douban", "category": "tv_hot", "media_type": "tv"},
    {"key": "douban-chinese-weekly", "title": "华语高分剧集", "provider": "douban", "category": "tv_chinese_weekly", "media_type": "tv"},
    {"key": "douban-global-weekly", "title": "全球高分剧集", "provider": "douban", "category": "tv_global_weekly", "media_type": "tv"},
    {"key": "bangumi-calendar", "title": "每日放送", "provider": "bangumi", "category": "calendar", "media_type": "tv"},
)

_CATEGORY_MEDIA: dict[str, dict[str, set[str]]] = {
    "tmdb": {
        "trending_week": {"all"},
        "popular": {"movie", "tv"},
        "discover": {"movie", "tv"},
    },
    "douban": {
        "movie_showing": {"movie"}, "movie_soon": {"movie"},
        "movie_hot": {"movie"}, "movie_top250": {"movie"},
        "tv_hot": {"tv"}, "tv_chinese_weekly": {"tv"},
        "tv_global_weekly": {"tv"}, "recommend": {"movie", "tv"},
    },
    "bangumi": {"calendar": {"tv"}},
}

COLLECTION_CATEGORIES = {"tmdb": "discover", "douban": "recommend"}

_TMDB_MOVIE_GENRES = {
    "28": "动作", "12": "冒险", "16": "动画", "35": "喜剧",
    "80": "犯罪", "99": "纪录片", "18": "剧情", "10751": "家庭",
    "14": "奇幻", "36": "历史", "27": "恐怖", "10402": "音乐",
    "9648": "悬疑", "10749": "爱情", "878": "科幻", "10770": "电视电影",
    "53": "惊悚", "10752": "战争", "37": "西部",
}
_TMDB_TV_GENRES = {
    "10759": "动作冒险", "16": "动画", "35": "喜剧", "80": "犯罪",
    "99": "纪录片", "18": "剧情", "10751": "家庭", "10762": "儿童",
    "9648": "悬疑", "10763": "新闻", "10764": "真人秀",
    "10765": "科幻奇幻", "10766": "肥皂剧", "10767": "脱口秀",
    "10768": "战争政治", "37": "西部",
}
_LANGUAGES = {
    "zh": "中文", "en": "英语", "ja": "日语", "ko": "韩语",
    "fr": "法语", "de": "德语", "es": "西班牙语", "it": "意大利语",
    "pt": "葡萄牙语", "ru": "俄语", "hi": "印地语", "th": "泰语",
    "ar": "阿拉伯语",
}
_TMDB_MOVIE_SORTS = {
    "popularity.desc": "热度从高到低",
    "vote_average.desc": "评分从高到低",
    "primary_release_date.desc": "上映日期从新到旧",
}
_TMDB_TV_SORTS = {
    "popularity.desc": "热度从高到低",
    "vote_average.desc": "评分从高到低",
    "first_air_date.desc": "首播日期从新到旧",
}
_DOUBAN_SORTS = {
    "recommend": "热门推荐", "rank": "评分优先", "time": "时间优先",
}
_DOUBAN_MOVIE_TAGS = {"喜剧", "爱情", "动作", "科幻", "动画", "悬疑", "犯罪", "惊悚", "冒险", "奇幻", "恐怖", "战争", "武侠", "灾难"}
_DOUBAN_TV_TAGS = {
    "国产剧": "国产剧",
    "美剧": "美剧",
    "英剧": "英剧",
    "日剧": "日剧",
    "韩剧": "韩剧",
}
_WEEKDAYS = {
    "1": "星期一", "2": "星期二", "3": "星期三", "4": "星期四",
    "5": "星期五", "6": "星期六", "7": "星期日",
}

_FILTER_DEFAULTS: dict[tuple[str, str, str], dict[str, str]] = {
    ("tmdb", "discover", "movie"): {"with_genres": "", "with_original_language": "", "sort_by": "popularity.desc"},
    ("tmdb", "discover", "tv"): {"with_genres": "", "with_original_language": "", "sort_by": "popularity.desc"},
    ("douban", "recommend", "movie"): {"sort": "recommend", "tags": ""},
    ("douban", "recommend", "tv"): {"sort": "recommend", "tags": ""},
    ("bangumi", "calendar", "tv"): {"weekday": ""},
}


def validate_request(provider: str, category: str, media_type: str) -> tuple[str, str, str]:
    provider = (provider or "").strip().lower()
    category = (category or "").strip().lower()
    media_type = (media_type or "").strip().lower()
    categories = _CATEGORY_MEDIA.get(provider)
    if not categories:
        raise ValueError("不支持的数据源")
    allowed_media = categories.get(category)
    if not allowed_media:
        raise ValueError("不支持的榜单分类")
    if media_type not in allowed_media:
        raise ValueError("榜单分类与媒体类型不匹配")
    return provider, category, media_type


def validate_filters(provider: str, category: str, media_type: str,
                     filters: dict[str, Any] | None) -> dict[str, str]:
    provider, category, media_type = validate_request(provider, category, media_type)
    raw = {} if filters is None else filters
    if not isinstance(raw, dict):
        raise ValueError("筛选参数必须是对象")
    definition = (list_filter_definitions(provider, media_type)
                  if (provider, category, media_type) in _FILTER_DEFAULTS else {})
    allowed = {field["key"] for group in ("filters", "extra_filters")
               for field in definition.get(group, [])}
    date_field = "release_date" if media_type == "movie" else "first_air_date"
    unknown = set(raw) - allowed
    if unknown:
        raise ValueError(f"包含不支持的筛选项: {', '.join(sorted(unknown))}")

    result = {
        key: value.strip()
        for key, value in _FILTER_DEFAULTS.get((provider, category, media_type), {}).items()
        if value.strip()
    }
    result.update({key: str(value).strip() for key, value in raw.items() if str(value).strip()})
    if provider == "tmdb" and category == "discover":
        lower, upper = (result.get(f"{date_field}.{bound}", "") for bound in ("gte", "lte"))
        for value in (lower, upper):
            if value:
                if not re.fullmatch(r"(?:19|20)[0-9]{2}-[0-9]{2}-[0-9]{2}", value):
                    raise ValueError("TMDB 日期筛选无效")
                date.fromisoformat(value)
        if lower and upper and lower > upper:
            raise ValueError("TMDB 日期范围顺序无效")
        lower_score, upper_score = (float(result.get(f"vote_average.{bound}", default))
                                    for bound, default in (("gte", "0"), ("lte", "10")))
        if not 0 <= lower_score <= upper_score <= 10:
            raise ValueError("TMDB 评分范围必须在 0 到 10 且下限不大于上限")
        if "vote_count.gte" in result and not re.fullmatch(r"[0-9]+", result["vote_count.gte"]):
            raise ValueError("TMDB 最低投票人数必须是非负整数")
    if "with_genres" in result:
        genres = _TMDB_MOVIE_GENRES if media_type == "movie" else _TMDB_TV_GENRES
        values = [value.strip() for value in result["with_genres"].split(",") if value.strip()]
        if not values or any(value not in genres for value in values):
            raise ValueError("TMDB 类型筛选无效")
        result["with_genres"] = ",".join(values)
    for year_key in ("primary_release_year", "first_air_date_year"):
        if year_key in result and not re.fullmatch(r"(?:19|20)[0-9]{2}", result[year_key]):
            raise ValueError("TMDB 年份筛选无效")
    if "with_origin_country" in result:
        result["with_origin_country"] = result["with_origin_country"].upper()
        if not re.fullmatch(r"[A-Z]{2}", result["with_origin_country"]):
            raise ValueError("TMDB 产地筛选无效")
    if "with_original_language" in result:
        result["with_original_language"] = result["with_original_language"].lower()
    if "with_original_language" in result and result["with_original_language"] not in _LANGUAGES:
        raise ValueError("TMDB 原始语言筛选无效")
    if "sort_by" in result and result["sort_by"] not in {"popularity.desc", "vote_average.desc", "primary_release_date.desc", "first_air_date.desc"}:
        raise ValueError("TMDB 排序无效")
    if "sort" in result:
        result["sort"] = result["sort"].lower()
    if "sort" in result and result["sort"] not in _DOUBAN_SORTS:
        raise ValueError("豆瓣排序无效")
    if "tags" in result:
        tags = _DOUBAN_MOVIE_TAGS if media_type == "movie" else set(_DOUBAN_TV_TAGS)
        if result["tags"] not in tags:
            raise ValueError("豆瓣标签无效")
    if result.get("weekday") and result["weekday"] not in {str(value) for value in range(1, 8)}:
        raise ValueError("星期筛选无效")
    return {key: value for key, value in result.items() if value != ""}


def list_section_definitions(*, douban_enabled: bool | int | None = None) -> list[dict[str, Any]]:
    if douban_enabled is None:
        douban_enabled = config.get_bool("DISCOVERY_DOUBAN_ENABLED", True)
    sections = deepcopy(list(_SECTION_DEFINITIONS))
    for section in sections:
        section["enabled"] = bool(douban_enabled) if section["provider"] == "douban" else True
    return sections


def _filter_options(values: dict[str, str]) -> list[dict[str, str]]:
    return [{"value": value, "label": label} for value, label in values.items()]


def _plain_options(values: set[str]) -> list[dict[str, str]]:
    return [{"value": value, "label": value} for value in sorted(values)]


def list_catalog_definitions() -> list[dict[str, Any]]:
    """全部可查询栏目来自实际请求注册表；首页栏目只决定页面布局。"""
    douban_enabled = config.get_bool("DISCOVERY_DOUBAN_ENABLED", True)
    return [{"provider": provider, "category": category, "media_types": sorted(media_types),
             "enabled": douban_enabled if provider == "douban" else True,
             "supports_collection": COLLECTION_CATEGORIES.get(provider) == category}
            for provider, categories in _CATEGORY_MEDIA.items()
            for category, media_types in categories.items()]


def list_filter_definitions(provider: str, media_type: str) -> dict[str, Any]:
    provider = (provider or "").strip().lower()
    media_type = (media_type or "").strip().lower()
    extra_filters = []
    if provider == "tmdb" and media_type in {"movie", "tv"}:
        category = COLLECTION_CATEGORIES[provider]
        genres = _TMDB_MOVIE_GENRES if media_type == "movie" else _TMDB_TV_GENRES
        sorts = _TMDB_MOVIE_SORTS if media_type == "movie" else _TMDB_TV_SORTS
        fields = [("with_genres", "类型", "全部类型", genres),
                  ("with_original_language", "原始语言", "全部语言", _LANGUAGES),
                  ("sort_by", "排序", "默认排序", sorts)]
        date_field = "release_date" if media_type == "movie" else "first_air_date"
        year_field = "primary_release_year" if media_type == "movie" else "first_air_date_year"
        extra_filters = [
            {"key": "vote_average.gte", "label": "最低评分", "format": "0–10", "example": "7"},
            {"key": "vote_average.lte", "label": "最高评分", "format": "0–10", "example": "10"},
            {"key": "vote_count.gte", "label": "最低投票人数", "format": "非负整数", "example": "100"},
            {"key": "with_origin_country", "label": "产地", "format": "ISO 3166-1 alpha-2", "example": "CN"},
            {"key": year_field, "label": "年份", "format": "YYYY (1900–2099)", "example": "2020"},
            {"key": f"{date_field}.gte", "label": "起始日期", "format": "YYYY-MM-DD (1900–2099)", "example": "2016-01-01"},
            {"key": f"{date_field}.lte", "label": "截止日期", "format": "YYYY-MM-DD (1900–2099)", "example": "2026-12-31"},
        ]
    elif provider == "douban" and media_type in {"movie", "tv"}:
        category = COLLECTION_CATEGORIES[provider]
        tags = _DOUBAN_MOVIE_TAGS if media_type == "movie" else _DOUBAN_TV_TAGS
        fields = [("sort", "排序方式", "默认排序", _DOUBAN_SORTS),
                  ("tags", "类型 / 地区", "全部", tags)]
    elif provider == "bangumi" and media_type == "tv":
        category = "calendar"
        fields = [("weekday", "放送星期", "全部星期", _WEEKDAYS)]
    else:
        raise ValueError("不支持的数据源或媒体类型")
    return {
        "category": category,
        "filters": [{"key": key, "label": label, "all_label": all_label,
                     "options": _plain_options(values) if isinstance(values, set) else _filter_options(values)}
                    for key, label, all_label, values in fields],
        "extra_filters": extra_filters,
        "defaults": dict(_FILTER_DEFAULTS[(provider, category, media_type)]),
    }


# HTTP 入口仅识别参数名，具体栏目和值仍由 validate_filters 校验。
FILTER_QUERY_KEYS = frozenset(
    field["key"] for provider, _category, media_type in _FILTER_DEFAULTS
    for group in ("filters", "extra_filters")
    for field in list_filter_definitions(provider, media_type)[group]
)


class ProviderRegistry:
    def __init__(self, providers: dict[str, Any] | None = None):
        self._providers = {
            name.lower(): provider for name, provider in (providers or {}).items()
        }
        self._lock = threading.RLock()
        self._close_call_lock = threading.Lock()
        self._closing = False
        self._closed = False
        self._pending_close_providers: dict[int, Any] | None = None

    def register(self, provider: Any) -> None:
        name = str(getattr(provider, "name", "") or "").lower()
        if not name:
            raise ValueError("Provider name is required")
        with self._lock:
            if self._closed or self._closing:
                raise ProviderUnavailable("探索数据源注册表已关闭")
            self._providers[name] = provider

    def get(self, name: str) -> Any:
        with self._lock:
            if self._closed or self._closing:
                raise ProviderUnavailable("探索数据源注册表已关闭")
            provider = self._providers.get((name or "").lower())
        if provider is None:
            raise ProviderNotConfigured("数据源未配置")
        return provider

    def names(self) -> list[str]:
        with self._lock:
            return sorted(self._providers)

    def close(self) -> bool:
        """关闭所有唯一 Provider；失败项保留到下一次调用重试。"""
        with self._close_call_lock:
            with self._lock:
                if self._closed:
                    return True
                self._closing = True
                if self._pending_close_providers is None:
                    self._pending_close_providers = {
                        id(provider): provider for provider in self._providers.values()
                    }
                providers = tuple(self._pending_close_providers.values())

            failed: dict[int, Any] = {}
            for provider in providers:
                close = getattr(provider, "close", None)
                if not callable(close):
                    continue
                try:
                    closed = close()
                except Exception as exc:
                    logger.warning(
                        "关闭探索数据源失败 provider=%s error=%s",
                        getattr(provider, "name", type(provider).__name__),
                        exc,
                    )
                    failed[id(provider)] = provider
                    continue
                if closed is False:
                    logger.warning(
                        "关闭探索数据源未完成 provider=%s，已保留供后续重试",
                        getattr(provider, "name", type(provider).__name__),
                    )
                    failed[id(provider)] = provider

            with self._lock:
                self._pending_close_providers = failed
                if failed:
                    return False
                self._closed = True
                self._closing = False
                return True


def build_default_registry() -> ProviderRegistry:
    from app.discovery.providers.bangumi import BangumiProvider
    from app.discovery.providers.douban import DoubanProvider
    from app.discovery.providers.tmdb import TMDBProvider

    return ProviderRegistry({
        "tmdb": TMDBProvider(),
        "douban": DoubanProvider(),
        "bangumi": BangumiProvider(),
    })
