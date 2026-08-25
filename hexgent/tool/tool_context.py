"""工具执行期间共享的运行时上下文。"""

from dataclasses import dataclass, field
from pathlib import Path

from hexgent.plan import PlanStore


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

    def clear_read_file_cache(self) -> None:
        """在旧文件正文可能离开活动上下文后清空读取状态。"""
        self.read_file_cache.clear()

    def clear_tool_call_cache(self) -> None:
        """清空当前 Agent Run 的精确工具调用缓存和重复计数。"""
        self.tool_call_cache.clear()
        self.tool_call_duplicate_counts.clear()

    def clear_tool_caches(self) -> None:
        """在活动上下文失效时清空全部模型可见工具缓存。"""
        self.clear_read_file_cache()
        self.clear_tool_call_cache()
