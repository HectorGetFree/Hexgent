"""根据 Plan 状态审查一次正常模型响应是否可以结束。"""

from dataclasses import replace

from hexgent.events import AgentLoopResult
from hexgent.session import Session

from .limits import LoopLimits
from .loop_state import LoopState
from .transitions import Continue, Stop, TurnOutcome


def review_plan_exit(
    state: LoopState,
    limits: LoopLimits,
    output_text: str,
    session: Session | None = None,
) -> TurnOutcome:
    """仅依据当前 Plan 状态决定继续执行或形成明确终态。"""
    plan = state.tool_context.plan_store.get_plan()

    # 1. 没有活动 Plan 时，模型的有效无工具响应可以正常结束。
    if plan is None:
        return Stop(AgentLoopResult(output_text, state.elapsed_seconds))

    # 2. 已完成 Plan 在正常结束前清除，避免污染下一次用户请求。
    if plan.status == "completed":
        state.tool_context.plan_store.clear_plan()
        return Stop(AgentLoopResult(output_text, state.elapsed_seconds))

    # 3. 阻塞 Plan 保留在会话中，并形成可由用户恢复的明确终态。
    if plan.status == "blocked":
        return Stop(
            AgentLoopResult(
                output_text,
                state.elapsed_seconds,
                stop_reason="plan_blocked",
                status="blocked",
            )
        )

    # 4. 活动 Plan 仍有提醒额度时，构造携带明确原因的下一轮 State。
    if state.plan_completion_nudges < limits.max_plan_completion_nudges:
        message = {
            "role": "developer",
            "content": (
                "当前 Plan 仍处于 pending 或 in_progress，不能直接结束。请检查 Plan "
                "并选择一种真实状态：继续执行必要工作；调用 update_plan 更新进度；"
                "确认全部完成后标记 completed；确实无法继续时标记 blocked 并说明原因。"
            ),
        }
        state.context_manager.append_message(message)
        if session is not None:
            session.record_message(message)
        next_state = replace(
            state,
            plan_completion_nudges=state.plan_completion_nudges + 1,
            transition="plan_continuation",
        )
        return Continue(next_state, "plan_continuation")

    # 5. 提醒耗尽后不伪装成完成；保留 Plan，等待下一次用户输入恢复。
    return Stop(
        AgentLoopResult(
            output_text,
            state.elapsed_seconds,
            stop_reason="plan_still_active",
            status="incomplete",
        )
    )
