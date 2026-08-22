"""Agent Loop 单个轮次的状态迁移。"""

from collections.abc import Generator
from typing import Any

from ..client import get_model
from ..prompt import TOOLS
from ..task import task_needs_continuation
from ..tool.tool_execution import UserConfirmation, execute_tool_batch
from hexgent.context import build_context
from hexgent.events import (
    AgentEvent,
    AgentLoopResult,
    LoopLimitReachedEvent,
    TextDeltaEvent,
)
from .call_model import ModelCallError, call_model
from .limits import (
    LoopLimits,
    LoopStopReason,
    check_loop_limit,
    stop_reason_message,
)
from .state import LoopState
from .transitions import Continue, Stop, TurnOutcome


MAX_TASK_COMPLETION_NUDGES = 1


def _finalization_message(reason: LoopStopReason) -> dict[str, str]:
    """创建禁止继续调用工具的预算收尾要求。"""
    return {
        "role": "developer",
        "content": (
            f"{stop_reason_message(reason)}。现在进入本轮收尾阶段，不得继续调用工具。"
            "请简洁总结已经完成的工作、尚未完成的工作以及建议的下一步。"
            "不要因为本轮预算耗尽而把未完成的 Task 标记为 completed 或 blocked。"
        ),
    }


def _continue_with_finalization(
    state: LoopState,
    reason: LoopStopReason,
) -> Continue:
    """构造一次明确进入预算收尾的状态迁移。"""
    state.messages.append(_finalization_message(reason))
    next_state = LoopState(
        messages=state.messages,
        tool_context=state.tool_context,
        model_rounds=state.model_rounds,
        tool_calls=state.tool_calls,
        input_tokens=state.input_tokens,
        output_tokens=state.output_tokens,
        completion_nudges=state.completion_nudges,
        mode="finalizing",
        transition="budget_finalization",
        started_at=state.started_at,
        stop_reason=reason,
    )
    return Continue(next_state, "budget_finalization")


def _failure_stop(state: LoopState, message: str) -> Stop:
    """将模型调用错误转换为明确的失败 Terminal。"""
    return Stop(
        AgentLoopResult(
            output_text=f"错误：{message}",
            elapsed_seconds=state.elapsed_seconds,
            stop_reason=state.stop_reason,
            status="failed",
        )
    )


def run_turn(
    state: LoopState,
    client: Any,
    limits: LoopLimits,
    user_confirmation: UserConfirmation,
) -> Generator[AgentEvent, None, TurnOutcome]:
    """执行一个完整轮次，并只返回 Continue 或 Stop。"""
    # 1. 在每个正常轮次的唯一入口统一检查全局预算。
    if state.mode == "working":
        stop_reason = check_loop_limit(state, limits)
        if stop_reason is not None:
            yield LoopLimitReachedEvent(stop_reason)
            return _continue_with_finalization(state, stop_reason)

    # 2. 根据工作或收尾模式准备本轮模型请求。
    request: dict[str, Any] = {
        "model": get_model(),
        "instructions": build_context(state.tool_context),
        "input": state.messages,
    }
    if state.mode == "working":
        remaining_tokens = limits.max_total_tokens - state.total_tokens
        request["tools"] = TOOLS
        request["max_output_tokens"] = max(
            1,
            min(limits.max_output_tokens_per_round, remaining_tokens),
        )
    else:
        request["max_output_tokens"] = limits.finalization_output_tokens

    # 3. 模型调用必须产生完整响应，否则直接形成 Error Terminal。
    try:
        response = yield from call_model(client, **request)
    except ModelCallError as error:
        yield TextDeltaEvent(f"\n\n错误：{error}")
        return _failure_stop(state, str(error))

    # 4. 完整响应进入历史，并计算下一轮继承的资源用量。
    response_input_tokens, response_output_tokens = state.usage_values(
        getattr(response, "usage", None)
    )
    state.messages.extend(response.output)
    model_rounds = state.model_rounds + 1
    input_tokens = state.input_tokens + response_input_tokens
    output_tokens = state.output_tokens + response_output_tokens

    # 5. 收尾模式收到有效响应后直接形成限制 Terminal。
    if state.mode == "finalizing":
        task = state.tool_context.task_store.get_task()
        if task is not None and task.status == "completed":
            state.tool_context.task_store.clear_task()
        return Stop(
            AgentLoopResult(
                response.output_text,
                state.elapsed_seconds,
                state.stop_reason,
                "limit_reached",
            )
        )

    # 6. 没有工具调用时，根据 Task 状态继续提醒或正常停止。
    tool_calls = [item for item in response.output if item.type == "function_call"]
    if not tool_calls:
        task = state.tool_context.task_store.get_task()
        if (
            task_needs_continuation(task)
            and state.completion_nudges < MAX_TASK_COMPLETION_NUDGES
        ):
            state.messages.append(
                {
                    "role": "developer",
                    "content": (
                        "当前 Task 仍处于待处理或执行中。请继续完成必要工作，或调用 "
                        "update_task_plan 准确更新整体状态后再结束本轮任务。"
                    ),
                }
            )
            next_state = LoopState(
                messages=state.messages,
                tool_context=state.tool_context,
                model_rounds=model_rounds,
                tool_calls=state.tool_calls,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                completion_nudges=state.completion_nudges + 1,
                mode="working",
                transition="task_continuation",
                started_at=state.started_at,
                stop_reason=None,
            )
            return Continue(next_state, "task_continuation")

        if task is not None and task.status == "completed":
            state.tool_context.task_store.clear_task()
        return Stop(
            AgentLoopResult(
                response.output_text,
                state.elapsed_seconds,
                status="completed",
            )
        )

    # 7. 执行工具批次并构造下一轮 State，预算统一由下一轮入口判断。
    batch = yield from execute_tool_batch(
        tool_calls,
        context=state.tool_context,
        user_confirmation=user_confirmation,
        remaining_tool_calls=max(0, limits.max_tool_calls - state.tool_calls),
        deadline=state.started_at + limits.max_elapsed_seconds,
    )
    state.messages.extend(batch.outputs)
    next_state = LoopState(
        messages=state.messages,
        tool_context=state.tool_context,
        model_rounds=model_rounds,
        tool_calls=state.tool_calls + batch.executed_count,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        completion_nudges=state.completion_nudges,
        mode="working",
        transition="tool_results_ready",
        started_at=state.started_at,
        stop_reason=None,
    )
    return Continue(next_state, "tool_results_ready")
