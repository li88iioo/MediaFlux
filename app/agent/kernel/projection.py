"""一次执行、一次投影的工具结果协议。"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.agent.models import ToolReference
from app.sensitive_data import is_sensitive_key, redact_sensitive_text

from .state import StateUpdate

logger = logging.getLogger(__name__)

_SECRET_KEY_RE = re.compile(
    r"(?:api[_-]?key|token|secret|password|passwd|cookie|authorization|credential)",
    re.IGNORECASE,
)
_UNIX_PATH_RE = re.compile(r"(?<![A-Za-z0-9._~:/-])/(?:[^\s/]+/)+[^\s]*")
_WINDOWS_PATH_RE = re.compile(r"(?i)(?<![A-Za-z0-9_])(?:[a-z]:[\\/]|\\\\)[^\s]+")
_PRIVATE_MODEL_KEY_RE = re.compile(
    r"(?:^|[_-])(?:absolute[_-]?path|database[_-]?id|internal[_-]?id|row[_-]?id)(?:$|[_-])",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class ReferenceValue:
    kind: str
    value: Any
    ttl_seconds: int = 900


@dataclass(frozen=True, slots=True)
class ToolOutcome:
    model_content: str
    public_content: Mapping[str, Any]
    refs: tuple[ReferenceValue, ...] = ()
    state_updates: tuple[StateUpdate, ...] = ()
    telemetry: Mapping[str, Any] = field(default_factory=dict)
    effect_plan: Any | None = None

    def model_message(self) -> str:
        return self.model_content


def _json_safe(value: Any, *, depth: int = 0) -> Any:
    if depth > 10:
        return "[内容过深]"
    if value is None or isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, str):
        return redact_sensitive_text(value)[:2_000]
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, raw_value in list(value.items())[:200]:
            key = str(raw_key)[:160]
            result[key] = (
                "[已隐藏]"
                if is_sensitive_key(key) or _SECRET_KEY_RE.search(key)
                else _json_safe(raw_value, depth=depth + 1)
            )
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_safe(item, depth=depth + 1) for item in list(value)[:500]]
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            return _json_safe(to_dict(), depth=depth + 1)
        except Exception as exc:  # noqa: BLE001 - arbitrary domain DTO boundary
            logger.debug("Agent DTO 序列化降级 type=%s", type(exc).__name__)
            return redact_sensitive_text(value)[:500]
    return redact_sensitive_text(value)[:2_000]


def _model_safe(value: Any, *, depth: int = 0, key: str = "") -> Any:
    safe = _json_safe(value, depth=depth)
    if _PRIVATE_MODEL_KEY_RE.search(str(key or "")):
        return "[内部标识已隐藏；请使用返回的 opaque ref]"
    if isinstance(safe, str) and (
        _UNIX_PATH_RE.search(safe) or _WINDOWS_PATH_RE.search(safe)
    ):
        return "[内部路径已隐藏；请使用返回的 opaque ref]"
    if isinstance(safe, dict):
        return {
            child_key: _model_safe(item, depth=depth + 1, key=child_key)
            for child_key, item in safe.items()
        }
    if isinstance(safe, list):
        return [_model_safe(item, depth=depth + 1, key=key) for item in safe]
    return safe


class DefaultProjector:
    """把领域返回值投影为模型 DTO 与公开 DTO，不做二次 LLM 润色。"""

    def __init__(self, *, max_model_chars: int = 24_000) -> None:
        self.max_model_chars = max(2_000, int(max_model_chars))

    def project(self, value: Any) -> ToolOutcome:
        if isinstance(value, ToolOutcome):
            return value
        public = _json_safe(value)
        if not isinstance(public, dict):
            public = {
                "ok": True,
                "status": "success",
                "summary": str(public),
                "data": {},
            }
        public.setdefault("ok", True)
        public.setdefault("status", "success")
        public.setdefault("summary", "工具执行完成")
        to_model_dict = getattr(value, "to_model_dict", None)
        if callable(to_model_dict):
            try:
                model_payload = _model_safe(to_model_dict())
            except Exception as exc:  # noqa: BLE001 - arbitrary domain DTO boundary
                logger.debug("Agent 模型 DTO 序列化降级 type=%s", type(exc).__name__)
                model_payload = _model_safe(public)
        else:
            model_payload = _model_safe(public)
        model_content = json.dumps(
            model_payload,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        if len(model_content) > self.max_model_chars:
            summary = str(public.get("summary") or "工具执行完成")[:1_000]
            compact = {
                "ok": bool(public.get("ok")),
                "status": str(public.get("status") or "success")[:80],
                "summary": summary,
                "truncated": True,
            }
            model_content = json.dumps(
                compact, ensure_ascii=False, separators=(",", ":")
            )
        raw_references = getattr(value, "references", ())
        references: list[ReferenceValue] = []
        if isinstance(raw_references, Sequence) and not isinstance(
            raw_references, (str, bytes, bytearray)
        ):
            for item in raw_references:
                if not isinstance(item, ToolReference):
                    raise TypeError("tool result contains an invalid reference")
                references.append(
                    ReferenceValue(
                        kind=item.kind,
                        value=item.value,
                        ttl_seconds=item.ttl_seconds,
                    )
                )
        return ToolOutcome(
            model_content=model_content,
            public_content=public,
            refs=tuple(references),
        )
