"""统一媒体探索模块。"""
from .cache import CacheLookup, DiscoveryCache
from .models import (
    DiscoveryPage,
    MediaCard,
    ProviderAuthenticationError,
    ProviderError,
    ProviderHealth,
    ProviderInvalidResponse,
    ProviderNotConfigured,
    ProviderRateLimited,
    ProviderTimeout,
    ProviderUnavailable,
)

__all__ = [
    "CacheLookup", "DiscoveryCache", "DiscoveryPage", "MediaCard",
    "ProviderAuthenticationError", "ProviderError", "ProviderHealth",
    "ProviderInvalidResponse", "ProviderNotConfigured", "ProviderRateLimited",
    "ProviderTimeout", "ProviderUnavailable", "DiscoveryService", "get_discovery_service",
    "shutdown_discovery_service",
]


def __getattr__(name: str):
    if name in {"DiscoveryService", "get_discovery_service", "shutdown_discovery_service"}:
        from .service import DiscoveryService, get_discovery_service, shutdown_discovery_service
        return {
            "DiscoveryService": DiscoveryService,
            "get_discovery_service": get_discovery_service,
            "shutdown_discovery_service": shutdown_discovery_service,
        }[name]
    raise AttributeError(name)
