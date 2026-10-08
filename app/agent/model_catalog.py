"""Provider 模型列表服务；供 Telegram 与管理路由复用。"""

from __future__ import annotations

import json
import unicodedata

_MODEL_ID_MAX_LENGTH = 200


def normalize_provider_model_id(value: object) -> str:
    """返回可安全放入 Provider JSON 的模型 ID；拒绝所有控制字符。"""
    raw_model_id = str(value or "")
    if any(
        unicodedata.category(character) == "Cc" or character in "\u2028\u2029"
        for character in raw_model_id
    ):
        return ""
    model_id = raw_model_id.strip()
    if (
        not model_id
        or len(model_id) > _MODEL_ID_MAX_LENGTH
    ):
        return ""
    return model_id


async def fetch_ai_models(
    *, base_url: str, api_key: str, protocol: str, timeout_seconds: int
) -> list[str]:
    """从受限 Provider ``/models`` 端点读取可选模型 ID。"""
    from app.clients.openai_compatible import (
        normalize_provider_location,
        provider_headers,
        resolve_protocol,
    )
    from app.indexers.http import FixedHostHttpClient

    location = normalize_provider_location(
        base_url, https_only=True, public_only=True
    )
    resolved_protocol = resolve_protocol(protocol, base_url)
    headers = provider_headers(
        resolved_protocol, api_key, include_content_type=False
    )
    client = FixedHostHttpClient(
        allowed_hosts={location.host},
        timeout_seconds=timeout_seconds,
        max_response_bytes=512 * 1024,
        max_redirects=0,
        user_agent="MediaFlux-AI-Models/1.0",
        pin_resolved_address=True,
    )
    try:
        response = await client.get(
            location.models_url, headers=headers, max_redirects=0
        )
        if response.status_code != 200:
            raise ValueError(f"Provider /models 返回 HTTP {response.status_code}")
        envelope = json.loads(response.text)
        raw_models = envelope.get("data") if isinstance(envelope, dict) else None
        if not isinstance(raw_models, list):
            raise ValueError("Provider /models 响应格式无效")  # noqa: TRY004 - route contract maps provider shape errors to HTTP 400
        models: list[str] = []
        seen: set[str] = set()
        for item in raw_models[:1000]:
            model_id = normalize_provider_model_id(
                item.get("id") if isinstance(item, dict) else ""
            )
            if model_id and model_id not in seen:
                seen.add(model_id)
                models.append(model_id)
        return sorted(models, key=str.casefold)
    finally:
        await client.aclose()
