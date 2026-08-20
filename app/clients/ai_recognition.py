"""多协议 LLM Provider 结构化媒体识别客户端。"""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

import httpx
import requests

from app.clients.openai_compatible import (
    PROTOCOLS,
    SUPPORTED_PROTOCOLS_TEXT,
    extract_output_text,
    normalize_provider_location,
    protocol_attempts,
    provider_headers,
    resolve_protocol,
    structured_request_body,
)
from app.agent.async_bridge import (
    AsyncBridgeUnavailable,
    ensure_sync_bridge_available,
    run_awaitable_sync,
)
from app.logger import redact_sensitive_text
from app.modules.ai_recognition_governance import (
    AIRecognitionGovernanceError,
    acquire_ai_recognition_attempt,
    provider_fingerprint,
    record_ai_recognition_failure,
    record_ai_recognition_success,
)
from app.sensitive_data import contains_sensitive_credential


class AIRecognitionError(RuntimeError):
    """AI 服务不可用、响应不合法或结构校验失败。"""


class AIRecognitionProviderError(AIRecognitionError):
    """AI Provider 已被调用，但请求或响应不满足识别契约。"""


class AIRecognitionUnavailableError(AIRecognitionProviderError):
    """AI Provider 因网络、限流或服务端故障而暂时不可用。"""


def _assert_external_payload_safe(payload: dict[str, Any]) -> None:
    """外发前阻止文件名、目录名或别名中夹带凭据。"""
    pending: list[object] = list(payload.values())
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            pending.extend(value.values())
        elif isinstance(value, (list, tuple, set)):
            pending.extend(value)
        elif isinstance(value, str) and contains_sensitive_credential(value):
            raise AIRecognitionError("识别输入疑似包含凭据，已拒绝发送到外部 AI")


def _assert_provider_output_safe(payload: dict[str, Any]) -> None:
    """阻止 Provider 把疑似凭据带回诊断、缓存或后续 TMDB 查询。"""
    try:
        _assert_external_payload_safe(payload)
    except AIRecognitionError:
        raise AIRecognitionError("AI 返回内容疑似包含凭据，已拒绝使用") from None


@dataclass(frozen=True, slots=True)
class AIRecognitionInput:
    normalized_title: str
    filename_title: str = ""
    folder_title: str = ""
    folder_year: str = ""
    media_type: str = "movie"
    season: int | None = None
    episode: int | None = None
    aliases: tuple[str, ...] = ()

    def safe_payload(self) -> dict[str, Any]:
        payload = {
            "normalized_title": str(self.normalized_title or "")[:240],
            "filename_title": str(self.filename_title or "")[:240],
            "folder_title": str(self.folder_title or "")[:240],
            "folder_year": str(self.folder_year or "")[:4],
            "media_type": "tv" if self.media_type == "tv" else "movie",
            "season": self.season,
            "episode": self.episode,
            "aliases": [str(item)[:200] for item in self.aliases[:10]],
        }
        _assert_external_payload_safe(payload)
        return payload


@dataclass(frozen=True, slots=True)
class AIRecognitionResult:
    title: str
    original_title: str
    year: int | None
    media_type: str
    season: int | None
    episode: int | None
    aliases: tuple[str, ...]
    confidence: float

    def safe_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["aliases"] = list(self.aliases)
        _assert_provider_output_safe(payload)
        return payload


_RESULT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "title", "original_title", "year", "media_type",
        "season", "episode", "aliases", "confidence",
    ],
    "properties": {
        "title": {"type": "string"},
        "original_title": {"type": "string"},
        "year": {
            "type": ["integer", "null"],
            "minimum": 1870,
            "maximum": datetime.now().year + 2,
        },
        "media_type": {"type": "string", "enum": ["movie", "tv"]},
        "season": {"type": ["integer", "null"], "minimum": 0, "maximum": 200},
        "episode": {"type": ["integer", "null"], "minimum": 1, "maximum": 5000},
        "aliases": {"type": "array", "items": {"type": "string"}, "maxItems": 10},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
}
_ALLOWED_KEYS = set(_RESULT_SCHEMA["properties"])


def _bounded_int(value: object, *, minimum: int, maximum: int) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise AIRecognitionError("AI 返回的数字字段无效")
    if not minimum <= value <= maximum:
        raise AIRecognitionError("AI 返回的数字字段超出范围")
    return value


def _parse_result(payload: object) -> AIRecognitionResult:
    if not isinstance(payload, dict):
        raise AIRecognitionError("AI 返回结构不是 JSON 对象")
    # 模型提供的 TMDB ID 永远丢弃，后续必须重新搜索 TMDB。
    payload = dict(payload)
    payload.pop("tmdb_id", None)
    if set(payload) != _ALLOWED_KEYS:
        raise AIRecognitionError("AI 返回字段不完整或包含未知字段")
    _assert_provider_output_safe(payload)
    title_raw = payload.get("title")
    original_title_raw = payload.get("original_title")
    media_type_raw = payload.get("media_type")
    if not isinstance(title_raw, str) or not isinstance(original_title_raw, str):
        raise AIRecognitionError("AI 返回片名或原名类型无效")
    if not isinstance(media_type_raw, str):
        raise AIRecognitionError("AI 返回媒体类型无效")
    title = title_raw.strip()
    original_title = original_title_raw.strip()
    media_type = media_type_raw.strip()
    if not title or len(title) > 240:
        raise AIRecognitionError("AI 返回片名无效")
    if len(original_title) > 240 or media_type not in {"movie", "tv"}:
        raise AIRecognitionError("AI 返回媒体类型或原名无效")
    year = _bounded_int(
        payload.get("year"), minimum=1870, maximum=datetime.now().year + 2
    )
    season = _bounded_int(payload.get("season"), minimum=0, maximum=200)
    episode = _bounded_int(payload.get("episode"), minimum=1, maximum=5000)
    aliases_raw = payload.get("aliases")
    if not isinstance(aliases_raw, list) or any(
        not isinstance(item, str) for item in aliases_raw
    ):
        raise AIRecognitionError("AI 返回别名字段无效")
    if len(aliases_raw) > 10:
        raise AIRecognitionError("AI 返回别名数量超出范围")
    aliases: list[str] = []
    for item in aliases_raw[:10]:
        text = item.strip()
        if text and len(text) <= 200 and text.casefold() not in {
            value.casefold() for value in aliases
        }:
            aliases.append(text)
    confidence_raw = payload.get("confidence")
    if isinstance(confidence_raw, bool) or not isinstance(confidence_raw, (int, float)):
        raise AIRecognitionError("AI 返回置信度无效")
    confidence = float(confidence_raw)
    if not math.isfinite(confidence) or not 0 <= confidence <= 1:
        raise AIRecognitionError("AI 返回置信度超出范围")
    return AIRecognitionResult(
        title=title,
        original_title=original_title,
        year=year,
        media_type=media_type,
        season=season,
        episode=episode,
        aliases=tuple(aliases),
        confidence=round(confidence, 3),
    )


@dataclass(frozen=True, slots=True)
class AIReleaseGroupInput:
    group_token: str
    remainder_title: str

    def safe_payload(self) -> dict[str, str]:
        payload = {
            "group_token": str(self.group_token or "")[:120],
            "remainder_title": str(self.remainder_title or "")[:240],
        }
        _assert_external_payload_safe(payload)
        return payload


@dataclass(frozen=True, slots=True)
class AIReleaseGroupResult:
    is_release_group: bool
    canonical_name: str
    aliases: tuple[str, ...]
    confidence: float

    def safe_payload(self) -> dict[str, Any]:
        payload = {
            "is_release_group": self.is_release_group,
            "canonical_name": self.canonical_name,
            "aliases": list(self.aliases),
            "confidence": self.confidence,
        }
        _assert_provider_output_safe(payload)
        return payload


_RELEASE_GROUP_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["is_release_group", "canonical_name", "aliases", "confidence"],
    "properties": {
        "is_release_group": {"type": "boolean"},
        "canonical_name": {"type": "string"},
        "aliases": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
}


def _parse_release_group_result(payload: object) -> AIReleaseGroupResult:
    if not isinstance(payload, dict) or set(payload) != set(_RELEASE_GROUP_SCHEMA["properties"]):
        raise AIRecognitionError("AI 返回的发布组分类结构无效")
    _assert_provider_output_safe(payload)
    is_group = payload.get("is_release_group")
    canonical_raw = payload.get("canonical_name")
    aliases_raw = payload.get("aliases")
    confidence_raw = payload.get("confidence")
    if not isinstance(is_group, bool) or not isinstance(canonical_raw, str):
        raise AIRecognitionError("AI 返回的发布组分类字段无效")
    canonical = canonical_raw.strip()
    if is_group and (not canonical or len(canonical) > 120):
        raise AIRecognitionError("AI 返回的发布组名称无效")
    if not is_group and canonical:
        raise AIRecognitionError("AI 非发布组结论不应返回标准名称")
    if not isinstance(aliases_raw, list) or len(aliases_raw) > 8 or any(
        not isinstance(item, str) for item in aliases_raw
    ):
        raise AIRecognitionError("AI 返回的发布组别名无效")
    aliases: list[str] = []
    for raw in ([canonical] if canonical else []) + aliases_raw:
        value = str(raw or "").strip()
        if value and len(value) <= 120 and value.casefold() not in {item.casefold() for item in aliases}:
            aliases.append(value)
    if isinstance(confidence_raw, bool) or not isinstance(confidence_raw, (int, float)):
        raise AIRecognitionError("AI 返回的发布组置信度无效")
    confidence = float(confidence_raw)
    if not math.isfinite(confidence) or not 0 <= confidence <= 1:
        raise AIRecognitionError("AI 返回的发布组置信度超出范围")
    return AIReleaseGroupResult(
        is_release_group=is_group, canonical_name=canonical,
        aliases=tuple(aliases), confidence=round(confidence, 3),
    )


class AIRecognitionClient:
    """只发送规范化上下文，不发送路径、凭据、签名链接或 TMDB Key。"""

    def __init__(
        self,
        *,
        api_url: str,
        model: str,
        api_key: str = "",
        protocol: str | None = None,
        timeout_seconds: int = 15,
        proxy_url: str = "",
        session: requests.Session | Any | None = None,
    ) -> None:
        raw_url = str(api_url or "").strip()
        try:
            location = normalize_provider_location(
                raw_url, https_only=True, public_only=True
            )
        except ValueError as exc:
            raise AIRecognitionError(str(exc)) from exc
        raw_protocol = str(protocol or "").strip().lower().replace("-", "_")
        if raw_protocol and raw_protocol not in PROTOCOLS:
            raise AIRecognitionError(
                f"AI 接口协议仅支持 {SUPPORTED_PROTOCOLS_TEXT}"
            )
        configured_protocol = resolve_protocol(raw_protocol or "auto", raw_url)
        self.location = location
        self.base_url = location.base_url
        self.protocol = configured_protocol
        self.api_url = location.endpoint(
            "responses" if self.protocol == "auto" else self.protocol
        )
        self.model = str(model or "").strip()
        if not self.model or len(self.model) > 200:
            raise AIRecognitionError("未配置有效的 AI 模型")
        self.api_key = str(api_key or "").strip()
        self.timeout_seconds = max(2, min(int(timeout_seconds or 15), 60))
        # 注入 Session 仅作为单元测试/兼容适配缝；生产请求统一走固定公网 HTTPS 客户端。
        self.session = session
        if session is not None:
            proxy = str(proxy_url or "").strip()
            if proxy and hasattr(session, "proxies"):
                normalized = (
                    proxy
                    if proxy.startswith(("http://", "https://"))
                    else f"http://{proxy}"
                )
                session.proxies.update({"http": normalized, "https": normalized})
        self.provider_fingerprint = provider_fingerprint(
            base_url=self.base_url,
            model=self.model,
            api_key=self.api_key,
            protocol=self.protocol,
        )

    async def _post_fixed_host(
        self, api_url: str, *, protocol: str, body: dict[str, Any]
    ) -> Any:
        from app.indexers.http import FixedHostHttpClient

        client = FixedHostHttpClient(
            allowed_hosts={self.location.host},
            timeout_seconds=self.timeout_seconds,
            max_response_bytes=64 * 1024,
            max_redirects=0,
            user_agent="MediaFlux-AIRecognition/1.0",
            pin_resolved_address=True,
        )
        try:
            return await client.post_json(
                api_url,
                headers=provider_headers(protocol, self.api_key),
                json=body,
                max_redirects=0,
            )
        finally:
            await client.aclose()

    def _post(
        self, api_url: str, *, protocol: str, body: dict[str, Any]
    ) -> requests.Response | Any:
        if self.session is not None:
            return self.session.post(
                api_url,
                headers=provider_headers(protocol, self.api_key),
                json=body,
                timeout=(min(5, self.timeout_seconds), self.timeout_seconds),
                allow_redirects=False,
            )
        return run_awaitable_sync(
            self._post_fixed_host(api_url, protocol=protocol, body=body)
        )

    @staticmethod
    def _response_body(response: object) -> bytes:
        body = getattr(response, "body", None)
        if body is None:
            body = getattr(response, "content", b"")
        if isinstance(body, str):
            return body.encode("utf-8", "replace")
        return bytes(body or b"")

    @staticmethod
    def _response_json(response: object) -> object:
        parser = getattr(response, "json", None)
        if callable(parser):
            return parser()
        body = AIRecognitionClient._response_body(response)
        return json.loads(body.decode("utf-8", "replace"))

    def _request_structured(
        self,
        *,
        system_prompt: str,
        payload: dict[str, Any],
        schema_name: str,
        schema: dict[str, Any],
    ) -> object:
        _assert_external_payload_safe(payload)
        protocols = protocol_attempts(self.protocol)
        for index, protocol in enumerate(protocols):
            body = structured_request_body(
                protocol=protocol,
                model=self.model,
                system_prompt=system_prompt,
                user_content=json.dumps(payload, ensure_ascii=False),
                schema_name=schema_name,
                schema=schema,
                max_tokens=800,
            )
            api_url = self.location.endpoint(protocol)
            lease = None
            try:
                # 活动事件循环线程无法使用同步桥。必须在扣减分钟/日额度前检查，
                # 否则本地根本没有发出请求也会消耗用户预算。
                if self.session is None:
                    ensure_sync_bridge_available()
                    lease = acquire_ai_recognition_attempt(self.provider_fingerprint)
                response = self._post(api_url, protocol=protocol, body=body)
            except AIRecognitionGovernanceError as exc:
                raise AIRecognitionError(str(exc)) from None
            except AsyncBridgeUnavailable:
                # 同步桥不可用是本地调用环境问题，不应触发 Tavily 或 Provider 熔断。
                raise AIRecognitionError("当前执行环境无法同步调用 AI 识别服务") from None
            except (
                requests.exceptions.InvalidURL,
                requests.exceptions.InvalidSchema,
                requests.exceptions.TooManyRedirects,
                httpx.UnsupportedProtocol,
                httpx.LocalProtocolError,
                httpx.DecodingError,
                httpx.TooManyRedirects,
            ) as exc:
                if self.session is None:
                    record_ai_recognition_failure(
                        self.provider_fingerprint, provider_failure=False
                    )
                safe = redact_sensitive_text(exc)
                raise AIRecognitionProviderError(
                    f"AI 识别请求配置或协议无效：{safe[:240]}"
                ) from None
            except (
                requests.Timeout,
                requests.ConnectionError,
                httpx.TimeoutException,
                httpx.NetworkError,
                httpx.ProxyError,
                httpx.RemoteProtocolError,
            ):
                if self.session is None:
                    record_ai_recognition_failure(self.provider_fingerprint)
                raise AIRecognitionUnavailableError(
                    "AI 识别请求超时或连接失败"
                ) from None
            except (requests.RequestException, httpx.RequestError) as exc:
                # 未明确归类的客户端错误不得触发网页搜索兜底，避免把本地配置
                # 或协议错误误判成 Provider 宕机并继续消耗 Tavily 额度。
                if self.session is None:
                    record_ai_recognition_failure(
                        self.provider_fingerprint, provider_failure=False
                    )
                safe = redact_sensitive_text(exc)
                raise AIRecognitionProviderError(
                    f"AI 识别请求失败：{safe[:240]}"
                ) from None
            except Exception as exc:
                # FixedHostHttpClient 的域错误在此延迟边界统一收敛，避免包级循环导入。
                if self.session is not None:
                    raise
                try:
                    from app.indexers.errors import (
                        IndexerInvalidResponse,
                        IndexerRateLimited,
                        IndexerResponseTooLarge,
                        IndexerSecurityError,
                        IndexerTimeout,
                        IndexerUnavailable,
                        IndexerValidationError,
                    )
                except ImportError:  # pragma: no cover - 安装损坏时保留原始异常
                    raise
                if isinstance(exc, (IndexerUnavailable, IndexerTimeout, IndexerRateLimited)):
                    record_ai_recognition_failure(self.provider_fingerprint)
                    safe = redact_sensitive_text(
                        getattr(exc, "public_message", "AI 识别服务暂时不可用")
                    )
                    raise AIRecognitionUnavailableError(str(safe)[:240]) from None
                if isinstance(
                    exc,
                    (
                        IndexerSecurityError,
                        IndexerResponseTooLarge,
                        IndexerInvalidResponse,
                        IndexerValidationError,
                    ),
                ):
                    record_ai_recognition_failure(
                        self.provider_fingerprint, provider_failure=False
                    )
                    safe = redact_sensitive_text(
                        getattr(exc, "public_message", "AI 识别请求未通过安全校验")
                    )
                    raise AIRecognitionError(str(safe)[:240]) from None
                raise
            finally:
                if lease is not None:
                    lease.release()

            response_body = self._response_body(response)
            if len(response_body) > 64 * 1024:
                if self.session is None:
                    record_ai_recognition_failure(
                        self.provider_fingerprint, provider_failure=False
                    )
                raise AIRecognitionError("AI 响应超过 64KB 上限")
            status_code = int(getattr(response, "status_code", 0) or 0)
            headers = getattr(response, "headers", {}) or {}
            retry_after = headers.get("retry-after", "") if hasattr(headers, "get") else ""
            if status_code != 200:
                can_try_next = (
                    index + 1 < len(protocols)
                    and status_code in {404, 405, 415, 501}
                )
                if self.session is None:
                    record_ai_recognition_failure(
                        self.provider_fingerprint,
                        status_code=status_code,
                        retry_after=retry_after,
                        provider_failure=not can_try_next,
                    )
                if can_try_next:
                    continue
                if status_code == 429 or status_code >= 500:
                    raise AIRecognitionUnavailableError(
                        f"AI 服务返回 HTTP {status_code}"
                    )
                raise AIRecognitionProviderError(
                    f"AI 服务返回 HTTP {status_code}"
                )
            try:
                envelope = self._response_json(response)
                content = extract_output_text(envelope, protocol)
                parsed = json.loads(content)
            except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError):
                # 部分 OpenAI 兼容网关会对 /responses 返回 HTTP 200，
                # 但实际只完整支持 /chat/completions 的结构化输出。auto 模式下
                # 将这种“成功状态 + 无法解析协议响应”视为兼容性探测失败，
                # 有界回退到下一协议；显式协议仍保持严格失败，且绝不放宽 JSON。
                can_try_next = self.protocol == "auto" and index + 1 < len(protocols)
                if self.session is None:
                    record_ai_recognition_failure(
                        self.provider_fingerprint, provider_failure=False
                    )
                if can_try_next:
                    continue
                raise AIRecognitionError("AI 响应不是严格 JSON 结构") from None
            if self.session is None:
                record_ai_recognition_success(self.provider_fingerprint)
            return parsed
        raise AIRecognitionProviderError("AI 服务不支持已配置的接口协议")

    def recognize(self, value: AIRecognitionInput) -> AIRecognitionResult:
        parsed = self._request_structured(
            system_prompt=(
                "Normalize media identity only. Never invent or return a TMDB ID. "
                "Keep year between 1870 and the current year plus two, season between "
                "0 and 200, and episode between 1 and 5000; use null when unknown. "
                "Return the requested JSON schema and nothing else."
            ),
            payload=value.safe_payload(),
            schema_name="media_recognition",
            schema=_RESULT_SCHEMA,
        )
        return _parse_result(parsed)

    def classify_release_group(self, value: AIReleaseGroupInput) -> AIReleaseGroupResult:
        parsed = self._request_structured(
            system_prompt=(
                "Classify only whether group_token is an anime/video release, subtitle, "
                "encoding, or publishing group prefix. Bracketed work titles, episode titles, "
                "years, season labels, and technical tags are not release groups. "
                "Do not infer a group from remainder_title. Return strict JSON only."
            ),
            payload=value.safe_payload(),
            schema_name="release_group_classification",
            schema=_RELEASE_GROUP_SCHEMA,
        )
        return _parse_release_group_result(parsed)
