"""工具调用精确去重使用的缓存算法。"""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Any

from .tool_context import ToolCallCacheEntry, ToolExecutionContext


MAX_DUPLICATE_TOOL_CALLS = 3


def build_tool_call_fingerprint(
    tool_name: str,
    arguments: dict[str, Any],
) -> str:
    """根据工具名和规范化 JSON 参数构造稳定调用指纹。"""
    canonical_arguments = json.dumps(
        arguments,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    payload = f"{tool_name}:{canonical_arguments}".encode("utf-8")
    return sha256(payload).hexdigest()


def build_deduplicated_output(
    fingerprint: str,
    cached: ToolCallCacheEntry,
    context: ToolExecutionContext,
) -> tuple[str, bool]:
    """生成重复调用提示，并返回其是否应被视为错误。"""
    with context.cache_lock:
        duplicate_count = context.tool_call_duplicate_counts.get(fingerprint, 0) + 1
        context.tool_call_duplicate_counts[fingerprint] = duplicate_count
    if duplicate_count >= MAX_DUPLICATE_TOOL_CALLS:
        return (
            "错误：本次 Agent Run 中相同工具和参数的重复调用已达到上限，"
            "请使用此前结果或调整执行方案",
            True,
        )
    return (
        "<duplicate-tool-call\n"
        f'source_call_id="{cached.source_call_id}">\n'
        "相同工具和参数已经成功执行，请使用此前工具结果。\n"
        "</duplicate-tool-call>",
        False,
    )
