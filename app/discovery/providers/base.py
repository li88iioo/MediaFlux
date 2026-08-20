"""媒体探索 Provider 公共契约与 HTTP 错误映射。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

import requests

from app.discovery.models import (
    DiscoveryPage,
    MediaCard,
    ProviderAuthenticationError,
    ProviderInvalidResponse,
    ProviderRateLimited,
    ProviderTimeout,
    ProviderUnavailable,
)

TimeoutValue = float | tuple[float, float]
DEFAULT_TIMEOUT: tuple[float, float] = (5.0, 15.0)


def response_json(response: Any, *, expected_type: type = dict) -> Any:
    """校验 HTTP 状态与 JSON 顶层类型，并转换为结构化错误。"""
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        status = getattr(getattr(exc, "response", None), "status_code", 0) or 0
        if status in (401, 403):
            raise ProviderAuthenticationError("上游认证失败") from exc
        if status == 429:
            headers = getattr(getattr(exc, "response", None), "headers", {}) or {}
            try:
                retry_after = int(headers.get("Retry-After", 0) or 0)
            except (TypeError, ValueError):
                retry_after = 0
            raise ProviderRateLimited("上游请求受限", retry_after=retry_after) from exc
        raise ProviderUnavailable("上游服务不可用") from exc
    try:
        payload = response.json()
    except (TypeError, ValueError) as exc:
        raise ProviderInvalidResponse("上游返回无效 JSON") from exc
    if not isinstance(payload, expected_type):
        raise ProviderInvalidResponse("上游响应结构无效")
    return payload


def map_request_error(exc: Exception) -> ProviderUnavailable | ProviderTimeout:
    if isinstance(exc, requests.Timeout):
        return ProviderTimeout("上游请求超时")
    return ProviderUnavailable("上游连接失败")


class DiscoveryProvider(ABC):
    """Provider 的最小稳定接口。"""

    name = "unknown"

    @abstractmethod
    def list_items(
        self,
        category: str,
        media_type: str,
        page: int,
        filters: dict[str, Any] | None,
    ) -> DiscoveryPage:
        raise NotImplementedError

    @abstractmethod
    def get_detail(self, external_id: str, media_type: str) -> MediaCard:
        raise NotImplementedError
