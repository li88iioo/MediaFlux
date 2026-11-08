"""媒体探索领域模型与结构化错误。"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

_ALLOWED_MEDIA_TYPES = {"movie", "tv", "all"}
_ALLOWED_HEALTH = {"healthy", "degraded", "disabled", "unavailable", "not_configured"}


_BEARER_RE = re.compile(r"(?i)(\bBearer\s+)[A-Za-z0-9._~+/-]+")
_CREDENTIAL_RE = re.compile(
    r"(?i)((?:api[_-]?key|apikey|token|secret|authorization|_sig)\s*[=:]\s*)([^&\s]+)"
)


def redact_provider_message(message: str) -> str:
    value = _BEARER_RE.sub(r"\1********", message or "")
    return _CREDENTIAL_RE.sub(r"\1********", value)[:500]


def _retry_after_seconds(value: float | str | None) -> int:
    return max(0, int(value or 0))


def _calendar_weekday(value: float | str) -> int:
    weekday = int(value)
    if weekday < 1 or weekday > 7:
        raise ValueError("weekday must be between 1 and 7")
    return weekday


class ProviderError(RuntimeError):
    """可安全映射到 API 的上游错误；内部 detail 不返回客户端。"""

    code = "provider_error"
    status_code = 502

    def __init__(self, message: str, *, retry_after: int = 0, detail: str = ""):
        super().__init__(detail or message)
        self.safe_message = redact_provider_message(message)
        self.detail = detail or message or ""
        self.retry_after = _retry_after_seconds(retry_after)


class ProviderNotConfigured(ProviderError):
    code = "not_configured"
    status_code = 503


class ProviderUnavailable(ProviderError):
    code = "unavailable"
    status_code = 503


class ProviderRateLimited(ProviderError):
    code = "rate_limited"
    status_code = 429


class ProviderAuthenticationError(ProviderError):
    code = "authentication"
    status_code = 502


class ProviderTimeout(ProviderError):
    code = "timeout"
    status_code = 504


class ProviderInvalidResponse(ProviderError):
    request_attempted: bool = True
    code = "invalid_response"
    status_code = 502


@dataclass(frozen=True)
class ProviderHealth:
    name: str
    status: str = "healthy"
    message: str = ""
    retry_after: int = 0

    def __post_init__(self) -> None:
        if self.status not in _ALLOWED_HEALTH:
            raise ValueError(f"unsupported provider status: {self.status}")
        object.__setattr__(self, "retry_after", _retry_after_seconds(self.retry_after))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MediaCard:
    provider: str
    external_id: str
    media_type: str
    title: str
    original_title: str = ""
    year: str = ""
    overview: str = ""
    poster_key: str = ""
    backdrop_key: str = ""
    rating: float | None = None
    rating_source: str = ""
    release_date: str = ""
    weekday: int | None = None
    tmdb_id: str = ""
    douban_id: str = ""
    bangumi_id: str = ""
    state: str = "none"
    # 旧缓存、列表卡和不提供季集数据的来源保留 None，不伪造为 0 或空季列表。
    number_of_seasons: int | None = None
    number_of_episodes: int | None = None
    seasons: tuple[dict[str, Any], ...] | None = None
    # 仅保留来源已返回的事实；语言不能替代产地，旧缓存缺字段仍为未知。
    origin_country: tuple[str, ...] | None = None
    original_language: str | None = None
    genre_ids: tuple[int, ...] | None = None
    # 来源评分人数与热度是不同指标；缺失（含旧缓存）不是 0。
    vote_count: int | None = None
    popularity: float | None = None

    def __post_init__(self) -> None:
        provider = (self.provider or "").strip().lower()
        external_id = (self.external_id or "").strip()
        media_type = (self.media_type or "").strip().lower()
        if not provider:
            raise ValueError("provider is required")
        if not external_id:
            raise ValueError("external_id is required")
        if media_type not in _ALLOWED_MEDIA_TYPES:
            raise ValueError(f"unsupported media_type: {media_type}")
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "external_id", external_id)
        object.__setattr__(self, "media_type", media_type)
        object.__setattr__(self, "title", (self.title or "").strip())
        object.__setattr__(self, "tmdb_id", (self.tmdb_id or "").strip())
        object.__setattr__(self, "douban_id", (self.douban_id or "").strip())
        object.__setattr__(self, "bangumi_id", (self.bangumi_id or "").strip())
        if self.weekday is not None:
            object.__setattr__(self, "weekday", _calendar_weekday(self.weekday))
        if self.rating is not None:
            object.__setattr__(self, "rating", float(self.rating))
        if self.seasons is not None:
            object.__setattr__(self, "seasons", tuple(dict(item) for item in self.seasons))
        for name in ("origin_country", "genre_ids"):
            if (value := getattr(self, name)) is not None:
                object.__setattr__(self, name, tuple(value))

    @property
    def stable_id(self) -> str:
        return f"{self.provider}:{self.media_type}:{self.external_id}"

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["stable_id"] = self.stable_id
        for name in ("seasons", "origin_country", "genre_ids"):
            if result[name] is not None:
                result[name] = list(result[name])
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MediaCard":
        values = dict(data)
        values.pop("stable_id", None)
        for key in ("provider", "external_id", "media_type", "title", "tmdb_id", "douban_id", "bangumi_id"):
            if key in values:
                values[key] = str(values[key] or "")
        allowed = cls.__dataclass_fields__.keys()
        return cls(**{key: values[key] for key in allowed if key in values})


@dataclass(frozen=True)
class DiscoveryPage:
    items: tuple[MediaCard, ...] = field(default_factory=tuple)
    page: int = 1
    has_more: bool = False
    cached: bool = False
    stale: bool = False
    provider: ProviderHealth = field(default_factory=lambda: ProviderHealth(name="unknown"))

    def __post_init__(self) -> None:
        if self.page < 1:
            raise ValueError("page must be positive")
        object.__setattr__(self, "items", tuple(self.items or ()))

    def to_dict(self) -> dict[str, Any]:
        return {
            "items": [item.to_dict() for item in self.items],
            "page": self.page,
            "has_more": self.has_more,
            "cached": self.cached,
            "stale": self.stale,
            "provider": self.provider.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DiscoveryPage":
        provider = data.get("provider") or {}
        return cls(
            items=tuple(MediaCard.from_dict(item) for item in data.get("items") or []),
            page=int(data.get("page") or 1),
            has_more=bool(data.get("has_more")),
            cached=bool(data.get("cached")),
            stale=bool(data.get("stale")),
            provider=ProviderHealth(**provider),
        )
