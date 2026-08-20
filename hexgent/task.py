"""长任务执行所需的 Task 状态模型与存储。"""

from dataclasses import dataclass
from typing import Literal, Protocol


TaskStepStatus = Literal["pending", "in_progress", "completed"]
TaskStatus = Literal["pending", "in_progress", "completed", "blocked"]


@dataclass(frozen=True)
class TaskStep:
    """Task 中的一个可动态调整的执行步骤。"""

    content: str  # 描述整个步骤需要完成什么
    process_message: str  # 描述步骤执行时向用户展示的“正在……”信息
    status: TaskStepStatus


@dataclass(frozen=True)
class Task:
    """由一次用户需求产生的完整任务。"""

    objective: str
    status: TaskStatus
    steps: list[TaskStep]

# Protocol 表示结构化接口，任何对象只要实现这些方法就可以被视为 TaskStore。
class TaskStore(Protocol):
    """当前 Task 的存储接口。"""

    def get_task(self) -> Task | None:
        """返回当前 Task。"""

    def set_task(self, task: Task) -> None:
        """保存当前 Task。"""

    def clear_task(self) -> None:
        """清除当前 Task。"""


class InMemoryTaskStore:
    """仅在当前 Agent 会话中保存 Task 的内存实现。"""

    def __init__(self) -> None:
        self._task: Task | None = None

    def get_task(self) -> Task | None:
        """返回当前 Task。"""
        return self._task

    def set_task(self, task: Task) -> None:
        """原子替换当前 Task。"""
        self._task = task

    def clear_task(self) -> None:
        """清除当前 Task。"""
        self._task = None


def task_needs_continuation(task: Task | None) -> bool:
    """判断当前 Task 是否仍应由 Agent 继续执行。"""
    return task is not None and task.status in {"pending", "in_progress"}


def format_task_context(task: Task | None) -> str:
    """将当前 Task 格式化为提供给模型的运行时上下文。"""
    if task is None:
        return ""

    lines = [
        "当前 Task：",
        f"目标：{task.objective}",
        f"整体状态：{task.status}",
        "执行步骤：",
    ]
    for index, step in enumerate(task.steps, start=1):
        lines.append(f"{index}. [{step.status}] {step.content}")
    lines.append("目标不可改写；请按照实际进度调用 update_task_plan 动态更新步骤和整体状态。")
    return "\n".join(lines)
