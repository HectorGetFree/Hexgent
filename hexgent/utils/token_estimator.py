"""不依赖特定模型分词器的保守 Token 估算。"""

import json
from math import ceil
from typing import Any


def _serializable(value: Any) -> Any:
    """将 SDK 对象转换为适合稳定估算的基础数据。"""
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump()
    if hasattr(value, "__dict__"):
        return vars(value)
    return value


def estimate_value_tokens(value: Any) -> int:
    """根据中英文字符组成估算一个值占用的 Token 数量。"""
    try:
        text = json.dumps(
            value,
            ensure_ascii=False,
            default=_serializable,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, RecursionError):
        text = str(value)

    ascii_characters = sum(1 for character in text if character.isascii())
    non_ascii_characters = len(text) - ascii_characters
    return max(1, ceil(ascii_characters / 4) + non_ascii_characters)


def estimate_request_tokens(
    instructions: str,
    messages: list[Any],
    tools: list[dict[str, Any]],
) -> int:
    """估算下一次完整模型请求的输入 Token 数量。"""
    return (
        estimate_value_tokens(instructions)
        + estimate_value_tokens(messages)
        + estimate_value_tokens(tools)
    )
