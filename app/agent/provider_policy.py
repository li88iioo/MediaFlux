"""Provider operation 参数 schema 的小型、拒绝优先校验器。"""
from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from app.agent.provider_models import ProviderGatewayError

_FORBIDDEN_ARGUMENT_KEYS = frozenset({
    "url", "base_url", "host", "hostname", "port", "scheme",
    "headers", "header", "authorization", "token", "api_key",
    "cookie", "cookies", "password", "username", "method", "endpoint",
})


def _error(message: str) -> ProviderGatewayError:
    return ProviderGatewayError(message, code="invalid_arguments")


def _validate_scalar(name: str, value: Any, schema: Mapping[str, Any]) -> Any:
    expected = str(schema.get("type") or "")
    if expected == "string":
        if not isinstance(value, str):
            raise _error(f"{name} 必须是字符串")
        normalized = value.strip()
        minimum = int(schema.get("minLength", 0) or 0)
        maximum = int(schema.get("maxLength", 4096) or 4096)
        if not minimum <= len(normalized) <= maximum:
            raise _error(f"{name} 长度不符合要求")
        allowed = schema.get("enum")
        if isinstance(allowed, list) and normalized not in allowed:
            raise _error(f"{name} 取值不受支持")
        return normalized
    if expected == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise _error(f"{name} 必须是整数")
        minimum = int(schema.get("minimum", -(2**63)))
        maximum = int(schema.get("maximum", 2**63 - 1))
        if not minimum <= value <= maximum:
            raise _error(f"{name} 超出允许范围")
        return value
    if expected == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise _error(f"{name} 必须是数字")
        normalized = float(value)
        if not math.isfinite(normalized):
            raise _error(f"{name} 必须是有限数字")
        minimum = float(schema.get("minimum", -math.inf))
        maximum = float(schema.get("maximum", math.inf))
        if not minimum <= normalized <= maximum:
            raise _error(f"{name} 超出允许范围")
        return normalized
    if expected == "boolean":
        if not isinstance(value, bool):
            raise _error(f"{name} 必须是布尔值")
        return value
    if expected == "array":
        if not isinstance(value, list):
            raise _error(f"{name} 必须是数组")
        minimum = int(schema.get("minItems", 0) or 0)
        maximum = int(schema.get("maxItems", 100) or 100)
        if not minimum <= len(value) <= maximum:
            raise _error(f"{name} 数量不符合要求")
        item_schema = schema.get("items") if isinstance(schema.get("items"), Mapping) else {}
        return [
            _validate_value(f"{name}[{index}]", item, item_schema)
            for index, item in enumerate(value)
        ]
    if expected == "object":
        if not isinstance(value, dict):
            raise _error(f"{name} 必须是对象")
        return validate_provider_arguments(schema, value)
    raise _error(f"{name} 使用了不支持的参数类型")


def _validate_value(name: str, value: Any, schema: Mapping[str, Any]) -> Any:
    if value is None and schema.get("nullable") is True:
        return None
    return _validate_scalar(name, value, schema)


def validate_provider_arguments(
    schema: Mapping[str, Any], arguments: dict[str, Any] | None
) -> dict[str, Any]:
    """校验 Provider 参数；额外字段和网络/凭据控制字段默认拒绝。"""
    if not isinstance(arguments, dict):
        raise _error("Provider arguments 必须是对象")
    if str(schema.get("type") or "object") != "object":
        raise _error("Provider operation schema 无效")
    properties = schema.get("properties")
    if not isinstance(properties, Mapping):
        properties = {}
    required = {
        str(value) for value in schema.get("required", [])
        if isinstance(value, str)
    }
    extra = set(arguments) - set(properties)
    if extra or set(arguments) & _FORBIDDEN_ARGUMENT_KEYS:
        raise _error("Provider arguments 包含未开放字段")
    missing = required - set(arguments)
    if missing:
        raise _error(f"缺少必要参数：{', '.join(sorted(missing))}")
    normalized: dict[str, Any] = {}
    for name, property_schema in properties.items():
        if not isinstance(property_schema, Mapping):
            raise _error("Provider operation schema 无效")
        if name in arguments:
            normalized[name] = _validate_value(name, arguments[name], property_schema)
        elif "default" in property_schema:
            normalized[name] = _validate_value(name, property_schema["default"], property_schema)
    return normalized
