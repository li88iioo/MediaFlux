"""Provider 身份与模型列表服务；供内核、Telegram 与管理路由复用。"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Mapping

_MODEL_ID_MAX_LENGTH = 200


def provider_channel_fingerprint(values: Mapping[str, str]) -> str:
    """摘要化同一 Provider 快照；保持历史 KV 摘要格式，不保存 API key。"""
    base_url = (values.get("AGENT_LLM_API_URL", "") or "").strip().rstrip("/")
    protocol = (values.get("AGENT_LLM_PROTOCOL", "auto") or "auto").strip().casefold()
    api_key = (values.get("AGENT_LLM_API_KEY", "") or "")
    credential_digest = hashlib.sha256(api_key.encode("utf-8")).digest()
    material = b"\0".join((
        b"mediaflux-telegram-model-channel:v1",
        base_url.encode("utf-8"),
        protocol.encode("utf-8"),
        credential_digest,
    ))
    return hashlib.sha256(material).hexdigest()


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
    """从管理员配置的 Provider ``/models`` 端点读取可选模型 ID。"""
    from app.clients.http import FixedHostHttpClient
    from app.clients.openai_compatible import (
        normalize_provider_location,
        provider_headers,
        resolve_protocol,
    )

    location = normalize_provider_location(base_url)
    resolved_protocol = resolve_protocol(protocol, base_url)
    headers = provider_headers(
        resolved_protocol, api_key, include_content_type=False
    )
    client = FixedHostHttpClient(
        allowed_hosts={location.host},
        configured_origin=location.base_url,
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
