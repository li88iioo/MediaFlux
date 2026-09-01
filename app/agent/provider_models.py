"""Agent Provider Gateway 的静态操作与执行结果模型。"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from app.agent.models import RiskLevel

ProviderValidator = Callable[[dict[str, Any]], dict[str, Any]]


class ProviderGatewayError(RuntimeError):
    """可安全投影到 Agent 响应的 Provider 失败。"""

    def __init__(
        self,
        message: str,
        *,
        code: str = "provider_unavailable",
        external_write_possible: bool = False,
    ):
        super().__init__(message)
        self.safe_message = str(message or "Provider 当前不可用")
        self.code = str(code or "provider_unavailable")
        # Transport 只有在真实外部写请求可能已发出时才设置该标记。
        # Gateway 据此区分可安全失败与必须人工核对的结果未知。
        self.external_write_possible = bool(external_write_possible)


@dataclass(frozen=True, slots=True)
class ProviderOperationSpec:
    """由服务端登记、不可由模型修改的 Provider 操作。"""

    operation_id: str
    provider: str
    description: str
    risk: RiskLevel
    parameters: Mapping[str, Any]
    result_kind: str
    reference_arguments: Mapping[str, str] = field(default_factory=dict)
    max_items: int = 32
    timeout_seconds: int = 15
    domains: tuple[str, ...] = ()
    examples: tuple[str, ...] = ()

    def public_dict(self) -> dict[str, Any]:
        properties = self.parameters.get("properties", {})
        required = {str(item) for item in self.parameters.get("required", [])}
        input_fields: list[dict[str, Any]] = []
        if isinstance(properties, Mapping):
            for name, raw_schema in properties.items():
                if not isinstance(raw_schema, Mapping):
                    continue
                field: dict[str, Any] = {
                    "name": str(name),
                    "type": str(raw_schema.get("type") or "string"),
                    "required": str(name) in required,
                }
                if isinstance(raw_schema.get("enum"), list):
                    field["allowed_values"] = list(raw_schema["enum"])[:16]
                for key in ("minimum", "maximum", "default"):
                    value = raw_schema.get(key)
                    if isinstance(value, (bool, int, float, str)):
                        field[key] = value
                input_fields.append(field)
        field_summaries: list[str] = []
        for field in input_fields:
            label = f"{field['name']}:{field['type']}"
            if field.get("required"):
                label += "(required)"
            elif "default" in field:
                label += f"(default={field['default']})"
            field_summaries.append(label)
        return {
            "operation": self.operation_id,
            "provider": self.provider,
            "description": self.description,
            "risk": self.risk.value,
            "input_schema": ", ".join(field_summaries) or "none",
            "limits": {
                "max_items": self.max_items,
                "timeout_seconds": self.timeout_seconds,
            },
        }


@dataclass(frozen=True, slots=True)
class ProviderProfileView:
    profile_ref: str
    provider: str
    label: str
    state: str

    def public_dict(self) -> dict[str, str]:
        return {
            "profile_ref": self.profile_ref,
            "provider": self.provider,
            "label": self.label,
            "state": self.state,
        }


@dataclass(slots=True)
class ProviderPayload:
    """Transport 返回给 Gateway 的内部结果；仍需经过投影。"""

    summary: str
    data: dict[str, Any]
    source: str
    suggestions: list[str] = field(default_factory=list)
    status: str = "success"
