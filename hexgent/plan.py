"""复杂目标执行所需的 Plan 状态模型与存储。"""

from dataclasses import dataclass
from typing import Literal, Protocol


PlanStepStatus = Literal[
    "pending",  # 步骤尚未开始执行。
    "in_progress",  # 步骤正在执行，且同一 Plan 中只能有一个进行中步骤。
    "completed",  # 步骤已经完成。
]
PlanStatus = Literal[
    "pending",  # Plan 已创建，但尚未进入执行状态。
    "in_progress",  # Plan 正在按步骤执行。
    "completed",  # Plan 的全部目标已经完成。
    "blocked",  # Plan 因明确阻塞原因无法继续执行。
]


@dataclass(frozen=True)
class PlanStep:
    """Plan 中的一个可动态调整的执行步骤。"""

    # 描述整个步骤需要完成的具体工作。
    content: str

    # 步骤执行时向用户展示的“正在……”信息。
    process_message: str

    # 当前步骤所处的执行状态。
    status: PlanStepStatus

    # 跨计划版本稳定标识同一个步骤。
    step_id: str = ""


@dataclass(frozen=True)
class Plan:
    """为完成一次复杂用户目标维护的动态执行计划。"""

    # 从用户需求提炼出的稳定整体目标，创建后不可改写。
    objective: str

    # 当前 Plan 的整体执行状态。
    status: PlanStatus

    # 为完成整体目标而维护的有序执行计划。
    steps: list[PlanStep]

    # 当前计划的版本号，每次成功更新后自动加一。
    revision: int = 1

    # 最近一次结构性重规划的原因；纯进度更新时为空。
    last_replan_reason: str | None = None

    # Plan 无法继续执行时记录的结构化阻塞原因。
    blocked_reason: str | None = None

# Protocol 表示结构化接口，任何对象只要实现这些方法就可以被视为 PlanStore。
class PlanStore(Protocol):
    """当前 Plan 的存储接口。"""

    def get_plan(self) -> Plan | None:
        """返回当前 Plan。"""

    def set_plan(self, plan: Plan) -> None:
        """保存当前 Plan。"""

    def clear_plan(self) -> None:
        """清除当前 Plan。"""


class InMemoryPlanStore:
    """仅在当前 Agent 会话中保存 Plan 的内存实现。"""

    def __init__(self) -> None:
        # 当前进程内唯一保存的 Plan；空值表示没有活动 Plan。
        self._plan: Plan | None = None

    def get_plan(self) -> Plan | None:
        """返回当前 Plan。"""
        return self._plan

    def set_plan(self, plan: Plan) -> None:
        """原子替换当前 Plan。"""
        self._plan = plan

    def clear_plan(self) -> None:
        """清除当前 Plan。"""
        self._plan = None


def plan_needs_continuation(plan: Plan | None) -> bool:
    """判断当前 Plan 是否仍应由 Agent 继续执行。"""
    return plan is not None and plan.status in {"pending", "in_progress"}


def format_plan_context(plan: Plan | None) -> str:
    """将当前 Plan 格式化为提供给模型的运行时上下文。"""
    if plan is None:
        return ""

    lines = [
        "当前 Plan：",
        f"目标：{plan.objective}",
        f"整体状态：{plan.status}",
        f"计划版本：{plan.revision}",
        "执行步骤：",
    ]
    for index, step in enumerate(plan.steps, start=1):
        lines.append(
            f"{index}. [{step.status}] {step.content}（step_id: {step.step_id}）"
        )
    if plan.last_replan_reason:
        lines.append(f"上次调整原因：{plan.last_replan_reason}")
    if plan.blocked_reason:
        lines.append(f"阻塞原因：{plan.blocked_reason}")
    lines.append(
        "目标不可改写；获得新信息后应反思计划是否仍然合理。仅在步骤状态变化、"
        "原假设失效或后续方案需要调整时调用 update_plan。"
    )
    return "\n".join(lines)
