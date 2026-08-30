"""TMDB 媒体探索 Provider。"""
from __future__ import annotations

import threading
from typing import Any
from urllib.parse import urlsplit

from app.clients.tmdb import TMDBClient
from app.discovery.models import (
    DiscoveryPage,
    MediaCard,
    ProviderHealth,
    ProviderInvalidResponse,
    ProviderUnavailable,
)
from app.discovery.providers.base import DiscoveryProvider

_DISCOVER_FILTERS = {
    "sort_by", "with_genres", "without_genres", "with_keywords", "without_keywords",
    "primary_release_year", "first_air_date_year", "release_date.gte", "release_date.lte",
    "first_air_date.gte", "first_air_date.lte", "vote_average.gte", "vote_average.lte",
    "vote_count.gte", "with_original_language", "region", "include_adult",
    "include_video", "with_watch_providers", "watch_region",
}


def _date(value: Any) -> str:
    return str(value or "").strip()


def _rating(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _poster_key(value: Any) -> str:
    path = str(value or "").strip()
    if not path:
        return ""
    parsed = urlsplit(path)
    if parsed.scheme or parsed.netloc or ".." in parsed.path.split("/"):
        return ""
    clean = parsed.path.lstrip("/")
    return clean


class TMDBProvider(DiscoveryProvider):
    name = "tmdb"

    def __init__(self, client: TMDBClient | None = None):
        self.client = client or TMDBClient()
        self._close_lock = threading.Lock()
        self._closed = False

    def _ensure_open(self) -> None:
        with self._close_lock:
            if self._closed:
                raise ProviderUnavailable("TMDB 数据源已关闭")

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        close = getattr(self.client, "close", None)
        if callable(close):
            close()

    def list_items(
        self,
        category: str,
        media_type: str,
        page: int,
        filters: dict[str, Any] | None,
    ) -> DiscoveryPage:
        self._ensure_open()
        category = str(category or "").strip().lower()
        media_type = str(media_type or "").strip().lower()
        page = max(1, int(page))
        params: dict[str, Any] = {"page": page}

        if category in {"trending", "weekly_trending", "trending_week"}:
            path = "/trending/all/week"
            mixed = True
        elif category == "popular" and media_type in {"movie", "tv"}:
            path = f"/{media_type}/popular"
            mixed = False
        elif category == "discover" and media_type in {"movie", "tv"}:
            path = f"/discover/{media_type}"
            params.update({key: value for key, value in (filters or {}).items()
                           if key in _DISCOVER_FILTERS and value not in (None, "")})
            mixed = False
        else:
            raise ProviderInvalidResponse("不支持的 TMDB 分类或媒体类型")

        payload = self.client.get(path, params)
        results = payload.get("results")
        if not isinstance(results, list):
            raise ProviderInvalidResponse("TMDB 列表响应结构无效")
        items: list[MediaCard] = []
        for raw in results:
            if not isinstance(raw, dict):
                continue
            item_type = str(raw.get("media_type") or media_type).lower() if mixed else media_type
            if item_type not in {"movie", "tv"}:
                continue
            card = self._card(raw, item_type)
            if card:
                items.append(card)
        try:
            total_pages = int(payload.get("total_pages") or page)
        except (TypeError, ValueError):
            total_pages = page
        return DiscoveryPage(
            items=items,
            page=page,
            has_more=page < min(total_pages, 100),
            provider=ProviderHealth(name=self.name),
        )

    def get_detail(self, external_id: str, media_type: str) -> MediaCard:
        self._ensure_open()
        media_type = "tv" if media_type == "tv" else "movie"
        payload = self.client.detail(str(external_id or ""), media_type)
        card = self._card(payload, media_type)
        if card is None:
            raise ProviderInvalidResponse("TMDB 详情响应结构无效")
        return card

    @staticmethod
    def _card(raw: dict[str, Any], media_type: str) -> MediaCard | None:
        external_id = str(raw.get("id") or "").strip()
        if not external_id:
            return None
        primary_title = raw.get("name") if media_type == "tv" else raw.get("title")
        title = str(primary_title or raw.get("title") or raw.get("name") or "").strip()
        primary_original = (
            raw.get("original_name") if media_type == "tv" else raw.get("original_title")
        )
        original_title = str(
            primary_original or raw.get("original_title") or raw.get("original_name") or ""
        ).strip()
        release_date = _date(raw.get("first_air_date") if media_type == "tv" else raw.get("release_date"))
        return MediaCard(
            provider="tmdb",
            external_id=external_id,
            media_type=media_type,
            title=title,
            original_title=original_title,
            year=release_date[:4] if release_date else "",
            overview=str(raw.get("overview") or "").strip(),
            poster_key=_poster_key(raw.get("poster_path")),
            backdrop_key=_poster_key(raw.get("backdrop_path")),
            rating=_rating(raw.get("vote_average")),
            rating_source="tmdb",
            release_date=release_date,
            tmdb_id=external_id,
        )
