"""媒体探索 Provider 实现（使用惰性导入避免 Client/Provider 循环依赖）。"""
from __future__ import annotations

from typing import Any

from .base import DEFAULT_TIMEOUT, DiscoveryProvider

__all__ = [
    "BangumiProvider",
    "DEFAULT_TIMEOUT",
    "DiscoveryProvider",
    "DoubanProvider",
    "TMDBProvider",
]


def __getattr__(name: str) -> Any:
    if name == "BangumiProvider":
        from .bangumi import BangumiProvider
        return BangumiProvider
    if name == "DoubanProvider":
        from .douban import DoubanProvider
        return DoubanProvider
    if name == "TMDBProvider":
        from .tmdb import TMDBProvider
        return TMDBProvider
    raise AttributeError(name)
