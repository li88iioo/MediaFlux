from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Iterable

from .errors import IndexerValidationError

_QUERY_WHITESPACE = re.compile(r"\s+")
_DOWNLOAD_STATES = frozenset({"ready", "resolvable", "unavailable"})
_DOWNLOAD_KINDS = frozenset({"magnet", "torrent"})


@dataclass(frozen=True, slots=True)
class IndexerSearchRequest:
    query: str
    page: int = 1

    @classmethod
    def create(cls, query: str, page: int = 1) -> "IndexerSearchRequest":
        normalized = _normalize_search_text(query, required=True)
        _validate_page(page)
        return cls(query=normalized, page=page)


@dataclass(frozen=True, slots=True)
class IndexerMediaSearchRequest:
    title: str
    original_title: str = ""
    english_title: str = ""
    aliases: tuple[str, ...] = ()
    year: int | None = None
    media_type: str = ""
    page: int = 1

    @classmethod
    def create(
        cls,
        *,
        title: str,
        original_title: str = "",
        english_title: str = "",
        aliases: Iterable[str] | None = None,
        year: int | str | None = None,
        media_type: str = "",
        page: int = 1,
    ) -> "IndexerMediaSearchRequest":
        normalized_title = _normalize_search_text(title, required=True)
        normalized_original = _normalize_search_text(original_title)
        normalized_english = _normalize_search_text(english_title)
        if isinstance(aliases, (str, bytes)):
            raise IndexerValidationError("aliases must be a list")
        raw_aliases = list(aliases or ())
        if len(raw_aliases) > 8:
            raise IndexerValidationError("aliases cannot contain more than 8 values")
        normalized_aliases: list[str] = []
        seen = {
            value.casefold()
            for value in (normalized_title, normalized_original, normalized_english)
            if value
        }
        for raw_alias in raw_aliases:
            alias = _normalize_search_text(raw_alias)
            if not alias or alias.casefold() in seen:
                continue
            seen.add(alias.casefold())
            normalized_aliases.append(alias)
        normalized_year = _normalize_media_year(year)
        normalized_media_type = str(media_type or "").strip().lower()
        if normalized_media_type not in {"", "movie", "tv", "anime"}:
            raise IndexerValidationError("invalid media type")
        _validate_page(page)
        return cls(
            title=normalized_title,
            original_title=normalized_original,
            english_title=normalized_english,
            aliases=tuple(normalized_aliases),
            year=normalized_year,
            media_type=normalized_media_type,
            page=page,
        )

    def cache_identity(self) -> tuple[str, str, str, tuple[str, ...], int | None, str]:
        return (
            self.title,
            self.original_title,
            self.english_title,
            self.aliases,
            self.year,
            self.media_type,
        )


def _normalize_search_text(value: object, *, required: bool = False) -> str:
    normalized = _QUERY_WHITESPACE.sub(" ", unicodedata.normalize("NFKC", str(value or ""))).strip()
    if required and not normalized:
        raise IndexerValidationError("title is required")
    if len(normalized) > 120:
        raise IndexerValidationError("title length cannot exceed 120")
    return normalized


def _normalize_media_year(value: int | str | None) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise IndexerValidationError("invalid media year")
    try:
        year = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise IndexerValidationError("invalid media year") from exc
    if not 1800 <= year <= 2200:
        raise IndexerValidationError("invalid media year")
    return year


def _validate_page(page: int) -> None:
    if isinstance(page, bool) or not isinstance(page, int) or not 1 <= page <= 100:
        raise IndexerValidationError("page must be between 1 and 100")


@dataclass(frozen=True, slots=True)
class IndexerCapabilities:
    pagination_supported: bool
    download_kinds: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        invalid = set(self.download_kinds) - _DOWNLOAD_KINDS
        if invalid:
            raise ValueError(f"unsupported download kinds: {sorted(invalid)}")


@dataclass(slots=True)
class IndexerItem:
    site_id: str
    site_name: str
    title: str
    result_id: str | None = None
    detail_url: str | None = None
    category: str | None = None
    size_text: str | None = None
    size_bytes: int | None = None
    seeders: int | None = None
    leechers: int | None = None
    downloads: int | None = None
    published_at: datetime | None = None
    download_state: str = "unavailable"
    download_kinds: tuple[str, ...] = ()
    magnet: str | None = None
    torrent_url: str | None = None
    relevance_score: int | None = None
    match_reasons: tuple[str, ...] = ()
    cluster_id: str | None = None
    cluster_size: int = 1

    def __post_init__(self) -> None:
        self.site_id = str(self.site_id).strip().lower()
        self.site_name = str(self.site_name).strip()
        self.title = str(self.title).strip()
        if not self.site_id or not self.site_name or not self.title:
            raise ValueError("site_id, site_name and title are required")
        if self.download_state not in _DOWNLOAD_STATES:
            raise ValueError("invalid download_state")
        invalid = set(self.download_kinds) - _DOWNLOAD_KINDS
        if invalid:
            raise ValueError(f"unsupported download kinds: {sorted(invalid)}")
        if self.size_bytes is not None and self.size_bytes < 0:
            raise ValueError("size_bytes cannot be negative")
        if self.relevance_score is not None:
            self.relevance_score = max(0, min(int(self.relevance_score), 100))
        self.match_reasons = tuple(dict.fromkeys(str(reason).strip() for reason in self.match_reasons if str(reason).strip()))
        self.cluster_id = str(self.cluster_id or "").strip() or None
        self.cluster_size = max(1, int(self.cluster_size or 1))

    def with_result_id(self, result_id: str) -> "IndexerItem":
        return replace(self, result_id=result_id)

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "result_id": self.result_id,
            "site_id": self.site_id,
            "site_name": self.site_name,
            "title": self.title,
            "category": self.category,
            "size_text": self.size_text,
            "size_bytes": self.size_bytes,
            "seeders": self.seeders,
            "leechers": self.leechers,
            "downloads": self.downloads,
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "download_state": self.download_state,
            "download_kinds": list(self.download_kinds),
            "relevance_score": self.relevance_score,
            "match_reasons": list(self.match_reasons),
            "cluster_id": self.cluster_id,
            "cluster_size": self.cluster_size,
        }


@dataclass(frozen=True, slots=True)
class IndexerSitePageState:
    pagination_supported: bool
    requested_page: int
    has_more: bool | None
    next_page: int | None


@dataclass(slots=True)
class IndexerPage:
    items: list[IndexerItem]
    page: int
    has_more: bool
    pagination_supported: bool


@dataclass(frozen=True, slots=True)
class IndexerProviderError:
    site_id: str
    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"site_id": self.site_id, "code": self.code, "message": self.message}


@dataclass(slots=True)
class AggregatedIndexerResult:
    query: str
    page: int
    items: list[IndexerItem]
    sites_attempted: tuple[str, ...]
    sites_succeeded: tuple[str, ...]
    site_item_counts: dict[str, int] = field(default_factory=dict)
    site_visible_counts: dict[str, int] = field(default_factory=dict)
    site_queries: dict[str, str] = field(default_factory=dict)
    site_attempt_counts: dict[str, int] = field(default_factory=dict)
    site_fallbacks: dict[str, str] = field(default_factory=dict)
    site_page_states: dict[str, IndexerSitePageState] = field(default_factory=dict)
    has_more: bool = False
    errors: list[IndexerProviderError] = field(default_factory=list)
    partial: bool = False
    cached: bool = False

    def clone(self, *, cached: bool | None = None) -> "AggregatedIndexerResult":
        return AggregatedIndexerResult(
            query=self.query,
            page=self.page,
            items=[replace(item) for item in self.items],
            sites_attempted=tuple(self.sites_attempted),
            sites_succeeded=tuple(self.sites_succeeded),
            site_item_counts=dict(self.site_item_counts),
            site_visible_counts=dict(self.site_visible_counts),
            site_queries=dict(self.site_queries),
            site_attempt_counts=dict(self.site_attempt_counts),
            site_fallbacks=dict(self.site_fallbacks),
            site_page_states=dict(self.site_page_states),
            has_more=self.has_more,
            errors=list(self.errors),
            partial=self.partial,
            cached=self.cached if cached is None else cached,
        )


@dataclass(frozen=True, slots=True)
class ResolvedDownload:
    kind: str
    value: str | bytes
    filename: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in _DOWNLOAD_KINDS:
            raise ValueError("invalid resolved download kind")


def download_kinds(*values: str | None) -> tuple[str, ...]:
    return tuple(value for value in values if value)
