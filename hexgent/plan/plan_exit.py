"""根据 Plan 状态形成不依赖 Agent Loop 的退出判断。"""

from dataclasses import dataclass
from typing import Literal

from .plan_model import Plan


PlanExitAction = Literal[
    "complete",  # 没有活动 Plan，或者 Plan 已经完成，可以正常结束。
    "blocked",  # Plan 已明确阻塞，应保留状态并结束当前 Run。
    "continue",  # Plan 仍在执行，应提醒模型继续完成或更新状态。
    "incomplete",  # 提醒额度耗尽，保留 Plan 并结束当前 Run。
]


@dataclass(frozen=True)
class PlanExitDecision:
    """Plan 领域对当前无工具响应能否结束的判断结果。"""

    # Loop 根据该动作选择正常结束、继续、阻塞或未完成终态。
    action: PlanExitAction
    # 需要继续时追加给下一轮模型的明确提醒；其他动作为空。
    continuation_message: str | None = None
    # 正常结束后是否需要清除已经完成的活动 Plan。
    clear_completed_plan: bool = False


def review_plan_exit(
    plan: Plan | None,
    completion_nudges: int,
    max_completion_nudges: int,
) -> PlanExitDecision:
    """仅依据 Plan 快照和提醒额度判断当前无工具响应的去向。"""
    # 1. 没有活动 Plan 时，模型的有效无工具响应可以正常结束。
    if plan is None:
        return PlanExitDecision("complete")
    # 2. 已完成 Plan 可以结束，并通知调用方清除活动状态。
    if plan.status == "completed":
        return PlanExitDecision("complete", clear_completed_plan=True)
    # 3. 阻塞 Plan 保留在会话中，由 Loop 形成可恢复终态。
    if plan.status == "blocked":
        return PlanExitDecision("blocked")
    # 4. 活动 Plan 仍有提醒额度时，要求模型继续或更新真实状态。
    if completion_nudges < max_completion_nudges:
        return PlanExitDecision(
            "continue",
            (
                "当前 Plan 仍处于 pending 或 in_progress，不能直接结束。请检查 Plan "
                "并选择一种真实状态：继续执行必要工作；调用 update_plan 更新进度；"
                "确认全部完成后标记 completed；确实无法继续时标记 blocked 并说明原因。"
            ),
        )
    # 5. 提醒耗尽后不伪装成完成，保留 Plan 等待后续用户输入。
    return PlanExitDecision("incomplete")
