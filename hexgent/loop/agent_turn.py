"""Agent Loop 单个轮次的状态迁移。"""

from collections.abc import Generator
from dataclasses import replace
from typing import Any

from ..client import get_model
from ..context.context_budget import ContextLimits
from ..context.compaction import compact_context_if_needed
from ..prompt import TOOLS
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
from .plan_exit import review_plan_exit
from .loop_state import LoopState
from .transitions import Continue, Stop, TurnOutcome


def _finalization_message(reason: LoopStopReason) -> dict[str, str]:
    """创建禁止继续调用工具的预算收尾要求。"""
    return {
        "role": "developer",
        "content": (
            f"{stop_reason_message(reason)}。现在进入本轮收尾阶段，不得继续调用工具。"
            "请简洁总结已经完成的工作、尚未完成的工作以及建议的下一步。"
            "不要因为本轮预算耗尽而把未完成的 Plan 标记为 completed 或 blocked。"
        ),
    }


def _continue_with_finalization(
    state: LoopState,
    reason: LoopStopReason,
) -> Continue:
    """构造一次明确进入预算收尾的状态迁移。"""
    state.conversation.append_message(_finalization_message(reason))
    next_state = LoopState(
        messages=state.messages,
        tool_context=state.tool_context,
        conversation=state.conversation,
        model_rounds=state.model_rounds,
        tool_calls=state.tool_calls,
        input_tokens=state.input_tokens,
        output_tokens=state.output_tokens,
        plan_completion_nudges=state.plan_completion_nudges,
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
    context_limits: ContextLimits | None = None,
) -> Generator[AgentEvent, None, TurnOutcome]:
    """执行一个完整轮次，并只返回 Continue 或 Stop。"""
    # 1. 在每个正常轮次的唯一入口统一检查全局预算。
    if state.mode == "working":
        stop_reason = check_loop_limit(state, limits)
        if stop_reason is not None:
            yield LoopLimitReachedEvent(stop_reason)
            return _continue_with_finalization(state, stop_reason)

    # 2. 根据工作或收尾模式准备本轮模型请求。
    model = get_model()
    instructions = build_context(state.tool_context)
    active_context_limits = context_limits or ContextLimits()

    # 3. 每次主模型调用前检查并压缩活动上下文，收尾轮也不能越过容量边界。
    request_tools = TOOLS if state.mode == "working" else []
    compaction = yield from compact_context_if_needed(
        state.conversation,
        client,
        model,
        instructions,
        request_tools,
        active_context_limits,
    )
    if compaction.status == "compacted":
        next_state = replace(
            state,
            messages=state.conversation.active_messages,
            input_tokens=state.input_tokens + compaction.input_tokens,
            output_tokens=state.output_tokens + compaction.output_tokens,
            transition="context_compacted",
        )
        return Continue(next_state, "context_compacted")
    if compaction.status == "blocked":
        return _failure_stop(state, compaction.message or "上下文无法安全压缩")

    # 4. 组装主模型请求；压缩摘要调用不计入正常模型轮次。
    request: dict[str, Any] = {
        "model": model,
        "instructions": instructions,
        "input": state.conversation.model_messages(),
    }
    if state.mode == "working":
        request["tools"] = TOOLS
        if limits.enforce_run_limits:
            remaining_tokens = limits.max_total_tokens - state.total_tokens
            request["max_output_tokens"] = max(
                1,
                min(limits.max_output_tokens_per_round, remaining_tokens),
            )
        else:
            request["max_output_tokens"] = limits.max_output_tokens_per_round
    else:
        request["max_output_tokens"] = limits.finalization_output_tokens

    # 5. 模型调用必须产生完整响应，否则直接形成 Error Terminal。
    try:
        response = yield from call_model(client, **request)
    except ModelCallError as error:
        state.conversation.record_runtime_event(
            "model_response_failed",
            {
                "mode": state.mode,
                "model_round": state.model_rounds + 1,
                "message": str(error),
            },
        )
        yield TextDeltaEvent(f"\n\n错误：{error}")
        return _failure_stop(state, str(error))

    # 6. 完整响应进入历史，并计算下一轮继承的资源用量。
    response_input_tokens, response_output_tokens = state.usage_values(
        getattr(response, "usage", None)
    )
    tool_calls = [item for item in response.output if item.type == "function_call"]
    state.conversation.record_runtime_event(
        "model_response_completed",
        {
            "mode": state.mode,
            "model_round": state.model_rounds + 1,
            "usage": getattr(response, "usage", None),
            "output_text": response.output_text,
            "tool_call_count": len(tool_calls),
        },
    )
    state.conversation.append_messages(
        response.output,
        completes_round=not tool_calls,
    )
    model_rounds = state.model_rounds + 1
    input_tokens = state.input_tokens + response_input_tokens
    output_tokens = state.output_tokens + response_output_tokens
    response_state = replace(
        state,
        model_rounds=model_rounds,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )

    # 7. 收尾模式收到有效响应后直接形成限制 Terminal。
    if state.mode == "finalizing":
        plan = state.tool_context.plan_store.get_plan()
        if plan is not None and plan.status == "completed":
            state.tool_context.plan_store.clear_plan()
        return Stop(
            AgentLoopResult(
                response.output_text,
                state.elapsed_seconds,
                state.stop_reason,
                "limit_reached",
            )
        )

    # 8. 没有工具调用时，统一交给 Plan 退出审查决定继续或终止。
    if not tool_calls:
        return review_plan_exit(response_state, limits, response.output_text)

    # 9. 执行工具批次并构造下一轮 State，预算统一由下一轮入口判断。
    batch = yield from execute_tool_batch(
        tool_calls,
        context=state.tool_context,
        user_confirmation=user_confirmation,
        remaining_tool_calls=(
            max(0, limits.max_tool_calls - state.tool_calls)
            if limits.enforce_run_limits
            else None
        ),
        deadline=(
            state.started_at + limits.max_elapsed_seconds
            if limits.enforce_run_limits
            else None
        ),
        session_store=state.conversation.session_store,
    )
    state.conversation.append_messages(batch.outputs, completes_round=True)
    next_state = LoopState(
        messages=state.messages,
        tool_context=state.tool_context,
        conversation=state.conversation,
        model_rounds=model_rounds,
        tool_calls=state.tool_calls + batch.executed_count,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        plan_completion_nudges=state.plan_completion_nudges,
        mode="working",
        transition="tool_results_ready",
        started_at=state.started_at,
        stop_reason=None,
    )
    return Continue(next_state, "tool_results_ready")
