"""工具执行期间共享的运行时上下文。"""

from dataclasses import dataclass
from pathlib import Path

from hexgent.task import TaskStore


@dataclass(frozen=True)
class ToolExecutionContext:
    """向有状态工具提供的稳定运行时依赖。"""

    task_store: TaskStore
    workdir: Path
