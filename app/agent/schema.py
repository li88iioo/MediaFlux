"""Agent 工具和 Provider 共用的有界 JSON Schema 校验。"""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from .errors import ToolPipelineError

_TYPE_CHECKS: dict[str, tuple[type, ...]] = {
    "object": (dict,),
    "array": (list, tuple),
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "null": (type(None),),
}

def _json_unique_key(value: Any) -> Any:
    """Hash JSON values without conflating booleans and numbers or object order."""
    if isinstance(value, dict):
        return ("object", frozenset((key, _json_unique_key(item)) for key, item in value.items()))
    if isinstance(value, (list, tuple)):
        return ("array", tuple(_json_unique_key(item) for item in value))
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return ("number", value)
    return (type(value), value)

def _is_quantity_cap(name: str) -> bool:
    return name == "limit" or name == "page_size" or name.endswith("_limit") or name.startswith("max_")


def normalize_read_arguments(arguments: dict[str, Any], schema: Mapping[str, Any]) -> dict[str, Any]:
    """只读查询的无歧义宽容：不改变查询对象的写法差异直接按规范值执行。

    - 数量上限超出时按上限执行：少取结果不改变查询对象，分页字段仍可续读；
      页码、季集号、偏移和编号等定位参数不在此列，越界照常报错。
    - 枚举值仅大小写不同且恰好对应一个选项时改用该选项（如 Movie → movie）。
    """
    properties = schema.get("properties") or {}
    normalized = dict(arguments)
    for key, value in arguments.items():
        child = properties.get(key)
        if not isinstance(child, Mapping):
            continue
        if (_is_quantity_cap(key) and child.get("type") == "integer"
                and isinstance(value, int) and not isinstance(value, bool)
                and child.get("maximum") is not None and value > child["maximum"]):
            normalized[key] = child["maximum"]
        elif isinstance(value, str) and isinstance(child.get("enum"), list) and value not in child["enum"]:
            matches = [option for option in child["enum"]
                       if isinstance(option, str) and option.casefold() == value.strip().casefold()]
            if len(matches) == 1:
                normalized[key] = matches[0]
    return normalized


def validate_json_schema(
    value: Any, schema: Mapping[str, Any], *, path: str = "arguments"
) -> None:
    def invalid(message: str) -> None:
        raise ToolPipelineError(message, code="invalid_arguments")

    schema_type = schema.get("type")
    types = [schema_type] if isinstance(schema_type, str) else schema_type
    if isinstance(types, list) and not any(
        isinstance(value, _TYPE_CHECKS.get(kind, ()))
        and not (kind in {"integer", "number"} and isinstance(value, bool))
        for kind in types
    ):
        invalid(f"{path} 类型无效")
    if "enum" in schema and not any(
        _json_unique_key(value) == _json_unique_key(option)
        for option in schema["enum"]
    ):
        options = "、".join(str(option) for option in list(schema["enum"])[:12])
        invalid(f"{path} 不在允许范围内，可选：{options}")
    if isinstance(value, str):
        if len(value) < int(schema.get("minLength", 0) or 0):
            invalid(f"{path} 太短，至少 {schema['minLength']} 个字符")
        maximum = schema.get("maxLength")
        if maximum is not None and len(value) > int(maximum):
            invalid(f"{path} 太长，最多 {maximum} 个字符")
        pattern = schema.get("pattern")
        if isinstance(pattern, str) and not re.search(pattern, value):
            invalid(f"{path} 格式无效")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if schema.get("minimum") is not None and value < schema["minimum"]:
            invalid(f"{path} 小于允许值，最小为 {schema['minimum']}")
        if schema.get("maximum") is not None and value > schema["maximum"]:
            invalid(f"{path} 超过允许值，最大为 {schema['maximum']}")
    if isinstance(value, dict):
        required = schema.get("required") or ()
        for key in required:
            if key not in value:
                invalid(f"缺少必需参数：{key}")
        properties = schema.get("properties") or {}
        if schema.get("additionalProperties") is False:
            unexpected = set(value).difference(properties)
            if unexpected:
                invalid(f"包含未知参数：{min(unexpected)}")
        for key, item in value.items():
            child = properties.get(key)
            if isinstance(child, Mapping):
                validate_json_schema(item, child, path=f"{path}.{key}")
        if schema.get("minProperties") is not None and len(value) < schema["minProperties"]:
            invalid(f"{path} 参数数量少于允许值 {schema['minProperties']}")
        if schema.get("maxProperties") is not None and len(value) > schema["maxProperties"]:
            if (path == "arguments" and schema["maxProperties"] == 1 and len(value) == 2
                    and all(key in properties and item is not None for key, item in value.items())):
                invalid(f"{path}：" + "、".join(sorted(value)) + " 只能提供其中一个")
            invalid(f"{path} 参数数量超过允许值 {schema['maxProperties']}")
    if isinstance(value, (list, tuple)):
        if isinstance(schema.get("items"), Mapping):
            for index, item in enumerate(value):
                validate_json_schema(item, schema["items"], path=f"{path}[{index}]")
        if schema.get("minItems") is not None and len(value) < schema["minItems"]:
            invalid(f"{path} 项数少于允许值 {schema['minItems']}")
        if schema.get("maxItems") is not None and len(value) > schema["maxItems"]:
            invalid(f"{path} 项数超过允许值 {schema['maxItems']}")
        if schema.get("uniqueItems") is True:
            seen = set()
            for index, item in enumerate(value):
                key = _json_unique_key(item)
                if key in seen:
                    invalid(f"{path}[{index}] 与此前项重复")
                seen.add(key)

    def matches(branch: Mapping[str, Any]) -> bool:
        try:
            validate_json_schema(value, branch, path=path)
        except ToolPipelineError:
            return False
        return True

    def invalid_combination(message: str, groups: Any) -> None:
        # 常见互斥字段给出具体反馈，其余组合直接返回 schema 约束。
        if path == "arguments" and isinstance(value, dict) and isinstance(groups, list):
            if len(groups) == 2 and all(
                isinstance(branch, Mapping) and set(branch) == {"required"}
                and isinstance(branch["required"], list) and len(branch["required"]) == 1
                and isinstance(branch["required"][0], str) for branch in groups
            ):
                keys = frozenset(branch["required"][0] for branch in groups)
                if len(keys) == 2 and all(value.get(key) is not None for key in keys):
                    invalid(message + "：" + "、".join(sorted(keys)) + " 只能提供其中一个")
        invalid(message)

    for keyword in ("oneOf", "anyOf"):
        branches = schema.get(keyword)
        if isinstance(branches, list):
            matched = sum(matches(branch) for branch in branches)
            if (keyword == "oneOf" and matched != 1) or (keyword == "anyOf" and not matched):
                invalid_combination(f"{path} 不符合 {keyword} 参数组合约束",
                                    branches if keyword == "oneOf" else None)
    forbidden = schema.get("not")
    if isinstance(forbidden, Mapping) and matches(forbidden):
        required = forbidden.get("required")
        groups = ([{"required": [key]} for key in required]
                  if set(forbidden) == {"required"} and isinstance(required, list) else None)
        if groups is None and path == "arguments" and isinstance(value, dict):
            def involved_fields(branch):
                fields = set(branch.get("required", ())) | set(branch.get("properties", {}))
                for keyword in ("allOf", "anyOf", "oneOf"):
                    for child in branch.get(keyword, ()):
                        if matches(child):
                            fields.update(involved_fields(child))
                return fields & value.keys() & schema.get("properties", {}).keys()
            fields = tuple(sorted(involved_fields(forbidden)))
            invalid(f"{path} 不允许此参数组合（not）；相关字段：" + "、".join(fields))
        invalid_combination(f"{path} 不允许此参数组合（not）", groups)
