"""工具执行期间共享的运行时上下文。"""

from dataclasses import dataclass, field
from pathlib import Path

from hexgent.plan import PlanStore


@dataclass(frozen=True)
class ReadFileCacheEntry:
    """一次成功文件读取留下的内容状态。"""

    # 上次成功返回给模型的完整文件内容 SHA-256。
    content_hash: str


@dataclass(frozen=True)
class ToolExecutionContext:
    """向有状态工具提供的稳定运行时依赖。"""

    # 当前会话用于读取和保存 Plan 的存储实现。
    plan_store: PlanStore

    # 文件和命令工具默认操作的工作区根目录。
    workdir: Path

    # 当前活动模型上下文中已经完整读取过的文件及其内容状态。
    read_file_cache: dict[Path, ReadFileCacheEntry] = field(default_factory=dict)

    def clear_read_file_cache(self) -> None:
        """在旧文件正文可能离开活动上下文后清空读取状态。"""
        self.read_file_cache.clear()
