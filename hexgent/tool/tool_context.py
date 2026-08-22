"""工具执行期间共享的运行时上下文。"""

from dataclasses import dataclass
from pathlib import Path

from hexgent.plan import PlanStore


@dataclass(frozen=True)
class ToolExecutionContext:
    """向有状态工具提供的稳定运行时依赖。"""

    # 当前会话用于读取和保存 Plan 的存储实现。
    plan_store: PlanStore

    # 文件和命令工具默认操作的工作区根目录。
    workdir: Path
