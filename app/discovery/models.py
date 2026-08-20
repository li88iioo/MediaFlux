"""媒体探索领域模型与结构化错误。"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import re
from typing import Any

_ALLOWED_MEDIA_TYPES = {"movie", "tv", "all"}
_ALLOWED_HEALTH = {"healthy", "degraded", "disabled", "unavailable", "not_configured"}


_BEARER_RE = re.compile(r"(?i)(\bBearer\s+)[A-Za-z0-9._~+/-]+")
_CREDENTIAL_RE = re.compile(
    r"(?i)((?:api[_-]?key|apikey|token|secret|authorization|_sig)\s*[=:]\s*)([^&\s]+)"
)


def redact_provider_message(message: str) -> str:
    value = _BEARER_RE.sub(r"\1********", str(message or ""))
    return _CREDENTIAL_RE.sub(r"\1********", value)[:500]


class ProviderError(RuntimeError):
    """可安全映射到 API 的上游错误；内部 detail 不返回客户端。"""

    code = "provider_error"
    status_code = 502

    def __init__(self, message: str, *, retry_after: int = 0, detail: str = ""):
        super().__init__(detail or message)
        self.safe_message = redact_provider_message(message)
        self.detail = str(detail or message or "")
        self.retry_after = max(0, int(retry_after or 0))


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
        object.__setattr__(self, "retry_after", max(0, int(self.retry_after or 0)))

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

    def __post_init__(self) -> None:
        provider = str(self.provider or "").strip().lower()
        external_id = str(self.external_id or "").strip()
        media_type = str(self.media_type or "").strip().lower()
        if not provider:
            raise ValueError("provider is required")
        if not external_id:
            raise ValueError("external_id is required")
        if media_type not in _ALLOWED_MEDIA_TYPES:
            raise ValueError(f"unsupported media_type: {media_type}")
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "external_id", external_id)
        object.__setattr__(self, "media_type", media_type)
        object.__setattr__(self, "title", str(self.title or "").strip())
        object.__setattr__(self, "tmdb_id", str(self.tmdb_id or "").strip())
        object.__setattr__(self, "douban_id", str(self.douban_id or "").strip())
        object.__setattr__(self, "bangumi_id", str(self.bangumi_id or "").strip())
        if self.weekday is not None:
            value = int(self.weekday)
            if value < 1 or value > 7:
                raise ValueError("weekday must be between 1 and 7")
            object.__setattr__(self, "weekday", value)
        if self.rating is not None:
            object.__setattr__(self, "rating", float(self.rating))

    @property
    def stable_id(self) -> str:
        return f"{self.provider}:{self.media_type}:{self.external_id}"

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["stable_id"] = self.stable_id
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MediaCard":
        values = dict(data)
        values.pop("stable_id", None)
        allowed = cls.__dataclass_fields__.keys()
        return cls(**{key: values.get(key) for key in allowed if key in values})


@dataclass(frozen=True)
class DiscoveryPage:
    items: tuple[MediaCard, ...] = field(default_factory=tuple)
    page: int = 1
    has_more: bool = False
    cached: bool = False
    stale: bool = False
    provider: ProviderHealth = field(default_factory=lambda: ProviderHealth(name="unknown"))

    def __post_init__(self) -> None:
        if int(self.page) < 1:
            raise ValueError("page must be positive")
        object.__setattr__(self, "page", int(self.page))
        object.__setattr__(self, "items", tuple(self.items or ()))

    def to_dict(self) -> dict[str, Any]:
        return {
            "items": [item.to_dict() for item in self.items],
            "page": self.page,
            "has_more": bool(self.has_more),
            "cached": bool(self.cached),
            "stale": bool(self.stale),
            "provider": self.provider.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DiscoveryPage":
        provider = data.get("provider") or {}
        return cls(
            items=[MediaCard.from_dict(item) for item in data.get("items") or []],
            page=int(data.get("page") or 1),
            has_more=bool(data.get("has_more")),
            cached=bool(data.get("cached")),
            stale=bool(data.get("stale")),
            provider=ProviderHealth(**provider),
        )
