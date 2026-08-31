"""工具执行期间共享的运行时上下文。"""

from dataclasses import dataclass, field
from hashlib import sha256
import json
from pathlib import Path
from threading import Lock
from typing import Any

from hexgent.plan import PlanStore


MAX_DUPLICATE_TOOL_CALLS = 3


@dataclass(frozen=True)
class ReadFileCacheKey:
    """唯一标识一次文件读取的路径和有效范围。"""

    # 文件在权限检查和工具执行中使用的绝对解析路径。
    path: Path

    # 分段读取的起始行；完整读取时为空。
    start_line: int | None = None

    # 分段读取的最大行数；完整读取时为空。
    line_count: int | None = None


@dataclass(frozen=True)
class ReadFileCacheEntry:
    """一次成功文件读取留下的内容状态。"""

    # 上次成功返回给模型的完整文件内容 SHA-256。
    content_hash: str


@dataclass(frozen=True)
class ToolCallCacheEntry:
    """一次可安全复用的成功工具调用结果。"""

    # 首次真实执行工具时由模型提供的调用标识。
    source_call_id: str

    # 首次真实执行后完整返回给模型的工具结果。
    output: str


@dataclass(frozen=True)
class ToolExecutionContext:
    """向有状态工具提供的稳定运行时依赖。"""

    # 当前会话用于读取和保存 Plan 的存储实现。
    plan_store: PlanStore

    # 文件和命令工具默认操作的工作区根目录。
    workdir: Path

    # 当前活动模型上下文中已经完整读取过的文件及其内容状态。
    read_file_cache: dict[ReadFileCacheKey, ReadFileCacheEntry] = field(
        default_factory=dict
    )

    # 当前 Agent Run 中可通过规范化参数指纹复用的成功工具调用。
    tool_call_cache: dict[str, ToolCallCacheEntry] = field(default_factory=dict)

    # 当前 Agent Run 中每个工具调用指纹已经命中的重复次数。
    tool_call_duplicate_counts: dict[str, int] = field(default_factory=dict)

    # 保护并行只读工具共享的缓存字典，不覆盖实际文件 I/O。
    cache_lock: Lock = field(default_factory=Lock, repr=False, compare=False)

    def clear_read_file_cache(self) -> None:
        """在旧文件正文可能离开活动上下文后清空读取状态。"""
        with self.cache_lock:
            self.read_file_cache.clear()

    def clear_tool_call_cache(self) -> None:
        """清空当前 Agent Run 的精确工具调用缓存和重复计数。"""
        with self.cache_lock:
            self.tool_call_cache.clear()
            self.tool_call_duplicate_counts.clear()

    def clear_tool_caches(self) -> None:
        """在活动上下文失效时清空全部模型可见工具缓存。"""
        self.clear_read_file_cache()
        self.clear_tool_call_cache()


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
