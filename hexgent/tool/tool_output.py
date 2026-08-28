"""工具成功输出的 JSON Schema 与专属校验。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


ToolOutputValidator = Callable[[dict[str, Any]], str | None]


def validate_json_output(data: Any, schema: dict[str, Any]) -> str | None:
    """校验工具结构化输出是否满足当前支持的 JSON Schema 子集。"""
    return _validate_value(data, schema, "data")


def _validate_value(value: Any, schema: dict[str, Any], path: str) -> str | None:
    """递归校验一个 JSON 值，并返回首个稳定中文错误。"""
    expected_type = schema.get("type")
    if isinstance(expected_type, list):
        if not any(_matches_type(value, item) for item in expected_type):
            return f"{path} 必须是 {expected_type} 类型"
    elif isinstance(expected_type, str) and not _matches_type(value, expected_type):
        return f"{path} 必须是 {expected_type} 类型"

    allowed_values = schema.get("enum")
    if isinstance(allowed_values, list) and value not in allowed_values:
        return f"{path} 必须是以下值之一：{allowed_values}"

    if isinstance(value, dict):
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        missing = [name for name in required if name not in value]
        if missing:
            return f"{path} 缺少字段：{', '.join(missing)}"
        if schema.get("additionalProperties") is False:
            extra = [name for name in value if name not in properties]
            if extra:
                return f"{path} 包含未声明字段：{', '.join(extra)}"
        for name, item in value.items():
            property_schema = properties.get(name)
            if property_schema is None:
                continue
            error = _validate_value(item, property_schema, f"{path}.{name}")
            if error is not None:
                return error

    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for index, item in enumerate(value):
            error = _validate_value(item, schema["items"], f"{path}[{index}]")
            if error is not None:
                return error
    return None


def _matches_type(value: Any, expected_type: str) -> bool:
    """判断 Python 值是否属于基础 JSON 类型。"""
    if expected_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    type_mapping: dict[str, type] = {
        "string": str,
        "boolean": bool,
        "object": dict,
        "array": list,
        "null": type(None),
    }
    expected_python_type = type_mapping.get(expected_type)
    return expected_python_type is None or isinstance(value, expected_python_type)
