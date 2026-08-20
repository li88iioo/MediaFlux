"""探索 Provider 注册表、栏目定义与筛选白名单。"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from app import config
from app.discovery.models import ProviderNotConfigured

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
    provider = str(provider or "").strip().lower()
    category = str(category or "").strip().lower()
    media_type = str(media_type or "").strip().lower()
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
    raw = filters or {}
    if not isinstance(raw, dict):
        raise ValueError("筛选参数必须是对象")
    allowed: set[str] = set()
    if provider == "tmdb" and category == "discover":
        allowed = {"with_genres", "with_original_language", "sort_by"}
    elif provider == "douban" and category == "recommend":
        allowed = {"sort", "tags"}
    elif provider == "bangumi" and category == "calendar":
        allowed = {"weekday"}
    unknown = set(raw) - allowed
    if unknown:
        raise ValueError(f"包含不支持的筛选项: {', '.join(sorted(unknown))}")

    result = {
        str(key): str(value).strip()
        for key, value in _FILTER_DEFAULTS.get((provider, category, media_type), {}).items()
        if str(value).strip()
    }
    result.update({str(key): str(value).strip() for key, value in raw.items() if str(value).strip()})
    if "with_genres" in result:
        genres = _TMDB_MOVIE_GENRES if media_type == "movie" else _TMDB_TV_GENRES
        values = [value.strip() for value in result["with_genres"].split(",") if value.strip()]
        if not values or any(value not in genres for value in values):
            raise ValueError("TMDB 类型筛选无效")
        result["with_genres"] = ",".join(values)
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


def list_section_definitions(*, douban_enabled: bool | None = None) -> list[dict[str, Any]]:
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


def list_filter_definitions(provider: str, media_type: str) -> dict[str, Any]:
    provider = str(provider or "").strip().lower()
    media_type = str(media_type or "").strip().lower()
    if provider == "tmdb" and media_type in {"movie", "tv"}:
        genres = _TMDB_MOVIE_GENRES if media_type == "movie" else _TMDB_TV_GENRES
        sorts = _TMDB_MOVIE_SORTS if media_type == "movie" else _TMDB_TV_SORTS
        return {
            "filters": [
                {
                    "key": "with_genres",
                    "label": "类型",
                    "all_label": "全部类型",
                    "options": _filter_options(genres),
                },
                {
                    "key": "with_original_language",
                    "label": "原始语言",
                    "all_label": "全部语言",
                    "options": _filter_options(_LANGUAGES),
                },
                {
                    "key": "sort_by",
                    "label": "排序",
                    "all_label": "默认排序",
                    "options": _filter_options(sorts),
                },
            ],
            "defaults": dict(_FILTER_DEFAULTS[("tmdb", "discover", media_type)]),
        }
    if provider == "douban" and media_type in {"movie", "tv"}:
        tags = _DOUBAN_MOVIE_TAGS if media_type == "movie" else _DOUBAN_TV_TAGS
        return {
            "filters": [
                {
                    "key": "sort",
                    "label": "排序方式",
                    "all_label": "默认排序",
                    "options": _filter_options(_DOUBAN_SORTS),
                },
                {
                    "key": "tags",
                    "label": "类型 / 地区",
                    "all_label": "全部",
                    "options": _plain_options(tags) if isinstance(tags, set) else _filter_options(tags),
                },
            ],
            "defaults": dict(_FILTER_DEFAULTS[("douban", "recommend", media_type)]),
        }
    if provider == "bangumi" and media_type == "tv":
        return {
            "filters": [
                {
                    "key": "weekday",
                    "label": "放送星期",
                    "all_label": "全部星期",
                    "options": _filter_options(_WEEKDAYS),
                },
            ],
            "defaults": dict(_FILTER_DEFAULTS[("bangumi", "calendar", "tv")]),
        }
    raise ValueError("不支持的数据源或媒体类型")


class ProviderRegistry:
    def __init__(self, providers: dict[str, Any] | None = None):
        self._providers = {str(name).lower(): provider for name, provider in (providers or {}).items()}

    def register(self, provider: Any) -> None:
        name = str(getattr(provider, "name", "") or "").lower()
        if not name:
            raise ValueError("Provider name is required")
        self._providers[name] = provider

    def get(self, name: str) -> Any:
        provider = self._providers.get(str(name or "").lower())
        if provider is None:
            raise ProviderNotConfigured("数据源未配置")
        return provider

    def names(self) -> list[str]:
        return sorted(self._providers)


def build_default_registry() -> ProviderRegistry:
    from app.discovery.providers.bangumi import BangumiProvider
    from app.discovery.providers.douban import DoubanProvider
    from app.discovery.providers.tmdb import TMDBProvider

    return ProviderRegistry({
        "tmdb": TMDBProvider(),
        "douban": DoubanProvider(),
        "bangumi": BangumiProvider(),
    })
