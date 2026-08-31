"""模型工具调用参数的解析与合法性校验。"""

from __future__ import annotations

import inspect
import json
from typing import Any

from hexgent.prompt import TOOLS

from .tool_context import ToolExecutionContext
from .tool_registry import RegisteredTool
from .tool_result import ToolExecutionResult, build_tool_failure_result


TOOL_SCHEMAS = {tool["name"]: tool["parameters"] for tool in TOOLS}


def _invalid(message: str) -> ToolExecutionResult:
    """创建参数无效结果。"""
    return build_tool_failure_result(
        "invalid",
        "invalid_arguments",
        "validation",
        message,
        side_effect_state="not_started",
    )


def _parse_arguments(raw_arguments: str) -> ToolExecutionResult | dict[str, Any]:
    """解析模型返回的 JSON 工具参数。"""
    try:
        arguments = json.loads(raw_arguments)
    except json.JSONDecodeError as error:
        return _invalid(f"工具参数不是有效的 JSON：{error}")
    except TypeError as error:
        return _invalid(f"工具参数无法解析：{error}")
    if not isinstance(arguments, dict):
        return _invalid("工具参数必须是 JSON 对象")
    return arguments


def _matches_json_type(value: Any, expected_type: str | list[str]) -> bool:
    """判断 Python 值是否符合基础 JSON Schema 类型。"""
    if isinstance(expected_type, list):
        return any(_matches_json_type(value, item) for item in expected_type)
    if expected_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    type_mapping: dict[str, type | tuple[type, ...]] = {
        "string": str,
        "boolean": bool,
        "object": dict,
        "array": list,
        "null": type(None),
    }
    expected_python_type = type_mapping.get(expected_type)
    return expected_python_type is None or isinstance(value, expected_python_type)


def _allows_null(property_schema: dict[str, Any]) -> bool:
    """判断字段是否使用可空类型表达模型侧的可选参数。"""
    expected_type = property_schema.get("type")
    return expected_type == "null" or (
        isinstance(expected_type, list) and "null" in expected_type
    )


def _validate_arguments(
    tool_name: str,
    tool: RegisteredTool,
    arguments: dict[str, Any],
    context: ToolExecutionContext,
) -> ToolExecutionResult | None:
    """按照工具 Schema、函数签名和专属规则校验参数。"""
    schema = TOOL_SCHEMAS.get(tool_name)
    if schema is None:
        return _invalid(f"工具“{tool_name}”缺少参数 Schema")
    properties = schema.get("properties", {})
    required = schema.get("required", [])
    missing = [
        name
        for name in required
        if name not in arguments and not _allows_null(properties.get(name, {}))
    ]
    if missing:
        return _invalid(f"缺少必填参数：{', '.join(missing)}")
    if schema.get("additionalProperties") is False:
        extra = [name for name in arguments if name not in properties]
        if extra:
            return _invalid(f"包含未声明的参数：{', '.join(extra)}")
    for name, value in arguments.items():
        expected_type = properties.get(name, {}).get("type")
        if expected_type and not _matches_json_type(value, expected_type):
            return _invalid(f"参数“{name}”必须是 {expected_type} 类型")
    try:
        bind_arguments = dict(arguments)
        if tool.requires_context:
            bind_arguments["context"] = context
        inspect.signature(tool.function).bind(**bind_arguments)
    except TypeError as error:
        return _invalid(f"参数与工具函数签名不匹配：{error}")
    tool_validation_error = tool.validate(arguments, context)
    if tool_validation_error is not None:
        return _invalid(tool_validation_error)
    return None


def parse_and_validate_tool_arguments(
    tool_name: str,
    tool: RegisteredTool,
    raw_arguments: str,
    context: ToolExecutionContext,
) -> ToolExecutionResult | dict[str, Any]:
    """解析原始参数，并在成功时返回经过完整校验的参数对象。"""
    parsed = _parse_arguments(raw_arguments)
    if isinstance(parsed, ToolExecutionResult):
        return parsed
    validation_error = _validate_arguments(tool_name, tool, parsed, context)
    if validation_error is not None:
        return validation_error
    return parsed
