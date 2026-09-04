"""Agent Loop 单个轮次的状态迁移。"""

from collections.abc import Generator
from dataclasses import asdict, replace
from typing import Any

from ..client import get_model
from ..config import HexgentConfig
from ..context.context_compaction import compact_context_if_needed
from ..prompt import TOOLS
from ..plan import review_plan_exit
from ..run_control import RunControl
from ..session import Session
from ..tool.tool_execution import UserConfirmation
from ..tool.tool_scheduler import execute_tool_batch
from ..tool.tool_permission_pipeline import PermissionContext
from hexgent.context import build_context
from hexgent.events import (
    AgentEvent,
    AgentLoopResult,
    LoopLimitReachedEvent,
    TextDeltaEvent,
)
from .call_model import ModelCallError, ModelCallInterrupted, call_model
from .limits import (
    LoopLimits,
    LoopStopReason,
    check_loop_limit,
    stop_reason_message,
)
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
    session: Session | None,
) -> Continue:
    """构造一次明确进入预算收尾的状态迁移。"""
    message = _finalization_message(reason)
    state.context_manager.append_message(message)
    if session is not None:
        session.record_context_messages(
            [message],
            completes_round=False,
            source="budget_finalization",
        )
    next_state = LoopState(
        context_manager=state.context_manager,
        tool_context=state.tool_context,
        relevant_context=state.relevant_context,
        model_rounds=state.model_rounds,
        tool_calls=state.tool_calls,
        input_tokens=state.input_tokens,
        output_tokens=state.output_tokens,
        plan_completion_nudges=state.plan_completion_nudges,
        reactive_compaction_attempted=state.reactive_compaction_attempted,
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
        ),
        state,
    )


def _apply_plan_exit(
    state: LoopState,
    limits: LoopLimits,
    output_text: str,
    session: Session | None,
) -> TurnOutcome:
    """把 Plan 领域判断转换为 Agent Loop 的 Continue 或 Stop。"""
    # 1. Plan 模块只返回领域判断，不直接依赖 LoopState 和状态迁移类型。
    decision = review_plan_exit(
        state.tool_context.plan_store.get_plan(),
        state.plan_completion_nudges,
        limits.max_plan_completion_nudges,
    )

    # 2. 正常完成时根据领域指示清除已完成 Plan。
    if decision.action == "complete":
        if decision.clear_completed_plan:
            state.tool_context.plan_store.clear_plan()
            if session is not None:
                session.record_plan_state(
                    None,
                    action="cleared",
                    reason="completed",
                )
        return Stop(AgentLoopResult(output_text, state.elapsed_seconds), state)

    # 3. 阻塞和提醒耗尽形成明确、可恢复的终态。
    if decision.action == "blocked":
        return Stop(
            AgentLoopResult(
                output_text,
                state.elapsed_seconds,
                stop_reason="plan_blocked",
                status="blocked",
            ),
            state,
        )
    if decision.action == "incomplete":
        return Stop(
            AgentLoopResult(
                output_text,
                state.elapsed_seconds,
                stop_reason="plan_still_active",
                status="incomplete",
            ),
            state,
        )

    # 4. 继续动作把领域提醒提交到 Context 和 Transcript，再构造完整下一轮 State。
    assert decision.continuation_message is not None
    message = {"role": "developer", "content": decision.continuation_message}
    state.context_manager.append_message(message)
    if session is not None:
        session.record_context_messages(
            [message],
            completes_round=False,
            source="plan_continuation",
        )
    return Continue(
        replace(
            state,
            plan_completion_nudges=state.plan_completion_nudges + 1,
            transition="plan_continuation",
        ),
        "plan_continuation",
    )


def _control_stop(
    state: LoopState,
    run_control: RunControl,
    session: Session | None,
    phase: str,
) -> Stop:
    """根据业务动作形成 Pause 或 Cancel 的明确终态。"""
    # 1. 兼容仅触发底层 InterruptController 的旧调用方，默认解释为 Pause。
    action = run_control.terminal_action or "pause"
    stop_reason = "user_cancel" if action == "cancel" else "user_pause"
    status = "cancelled" if action == "cancel" else "interrupted"

    # 2. Cancel 保留取消前快照作为事实，但从活动 PlanStore 清除执行义务。
    if action == "cancel":
        plan = state.tool_context.plan_store.get_plan()
        if plan is not None:
            if session is not None:
                session.record_runtime_event(
                    "plan_cancelled",
                    {
                        "reason": "用户取消当前任务",
                        "plan": asdict(plan),
                    },
                )
            state.tool_context.plan_store.clear_plan()
            if session is not None:
                session.record_plan_state(
                    None,
                    action="cleared",
                    reason="cancelled",
                )

    # 3. Session 记录业务动作和安全收束阶段，底层 Interrupt 不承载业务语义。
    if session is not None:
        session.record_runtime_event(
            "agent_run_controlled",
            {"action": action, "stop_reason": stop_reason, "phase": phase},
        )
    return Stop(
        AgentLoopResult(
            output_text="",
            elapsed_seconds=state.elapsed_seconds,
            stop_reason=stop_reason,
            status=status,
        ),
        state,
    )


def _continue_with_steering(
    state: LoopState,
    run_control: RunControl,
    session: Session | None,
    config: HexgentConfig,
) -> Continue | None:
    """在安全 Turn 边界按 FIFO 注入当前已到达的全部 Steering Query。"""
    # 1. drain 使用原子快照；调用期间新到达的 Query 留给下一个边界。
    queries = run_control.steering_mailbox.drain()
    if not queries:
        return None

    # 2. 每条 Query 保持独立原文进入 Context 和 Transcript，不合并、不摘要。
    applied_queries = []
    for query in queries:
        user_query = state.context_manager.append_user_query(query.content)
        applied_queries.append(user_query)
        if session is not None:
            session.record_user_query(user_query, source="steering")
            session.record_runtime_event(
                "steering_applied",
                {
                    "steering_sequence": query.sequence,
                    "query_sequence": user_query.sequence,
                    "before_model_round": state.model_rounds + 1,
                },
            )

    # 3. Steering 是当前 Run 内的新 Query，因此重新召回并固定相关历史记录。
    relevant_context = state.relevant_context
    if session is not None:
        relevant_context = session.retrieve_context_records(
            "\n".join(query.content for query in queries),
            tuple(query.sequence for query in applied_queries),
            config.context_retrieval,
        )

    # 4. 新用户要求重置 Plan 退出提醒，但保留同一 Run 的预算、耗时和 Plan。
    next_state = replace(
        state,
        relevant_context=relevant_context,
        plan_completion_nudges=0,
        transition="steering_applied",
    )
    return Continue(next_state, "steering_applied")


def run_turn(
    state: LoopState,
    *,
    config: HexgentConfig,
    client: Any,
    session: Session | None = None,
    user_confirmation: UserConfirmation,
    run_control: RunControl,
) -> Generator[AgentEvent, None, TurnOutcome]:
    """执行一个完整轮次，并只返回 Continue 或 Stop。"""
    # 1. 配置和外部依赖在整个 Run 内保持不变，LoopState 只承载活动状态。
    limits = config.loop
    active_control = run_control
    controller = active_control.interrupt_controller
    if controller.interrupted:
        return _control_stop(state, active_control, session, "turn_entry")

    # 2. 上一阶段结束后到达的 Steering Query 优先在新模型调用前进入 Context。
    steering_transition = _continue_with_steering(
        state,
        active_control,
        session,
        config,
    )
    if steering_transition is not None:
        return steering_transition

    # 3. 在每个正常轮次的唯一入口统一检查全局预算。
    if state.mode == "working":
        stop_reason = check_loop_limit(state, limits)
        if stop_reason is not None:
            yield LoopLimitReachedEvent(stop_reason)
            return _continue_with_finalization(state, stop_reason, session)

    # 4. 根据工作或收尾模式准备本轮模型请求。
    model = get_model()
    instructions = build_context(state.tool_context, state.relevant_context)
    active_context_limits = config.context

    # 5. 每次主模型调用前检查并压缩活动上下文，收尾轮也不能越过容量边界。
    request_tools = TOOLS if state.mode == "working" else []
    compaction = yield from compact_context_if_needed(
        state.context_manager,
        client,
        model,
        instructions,
        request_tools,
        active_context_limits,
    )
    if compaction.status in {"compacted", "partial"}:
        if compaction.boundary is not None and session is not None:
            session.record_compaction(compaction.boundary)
        state.tool_context.clear_tool_caches()
        next_state = replace(
            state,
            input_tokens=state.input_tokens + compaction.input_tokens,
            output_tokens=state.output_tokens + compaction.output_tokens,
            transition="context_compacted",
        )
        return Continue(next_state, "context_compacted")
    if compaction.status == "blocked":
        return _failure_stop(state, compaction.message or "上下文无法安全压缩")
    if controller.interrupted:
        return _control_stop(state, active_control, session, "context_preparation")

    # 6. 组装主模型请求；压缩摘要调用不计入正常模型轮次。
    request: dict[str, Any] = {
        "model": model,
        "instructions": instructions,
        "input": state.context_manager.model_messages(),
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

    # 7. 模型调用必须产生完整响应；上下文过长只允许应急压缩后重试一次。
    try:
        response = yield from call_model(
            client,
            interrupt_controller=controller,
            **request,
        )
    except ModelCallInterrupted as interrupted:
        if session is not None:
            session.record_runtime_event(
                "model_stream_interrupted",
                {
                    "action": active_control.terminal_action or "pause",
                    "model_round": state.model_rounds + 1,
                },
            )
        return _control_stop(state, active_control, session, "model_stream")
    except ModelCallError as error:
        can_recover = (
            error.reason == "prompt_too_long"
            and not state.reactive_compaction_attempted
        )
        if session is not None:
            session.record_runtime_event(
                "model_response_failed",
                {
                    "mode": state.mode,
                    "model_round": state.model_rounds + 1,
                    "message": str(error),
                    "reason": error.reason,
                    "recoverable": can_recover,
                },
            )

        # 8. 估算遗漏导致 prompt_too_long 时，使用更小近期预算强制压缩。
        if can_recover:
            reactive_limits = replace(
                active_context_limits,
                recent_context_tokens=(
                    active_context_limits.reactive_recent_context_tokens
                ),
            )
            reactive_compaction = yield from compact_context_if_needed(
                state.context_manager,
                client,
                model,
                instructions,
                request_tools,
                reactive_limits,
                force=True,
            )
            if reactive_compaction.status in {"compacted", "partial"}:
                if (
                    reactive_compaction.boundary is not None
                    and session is not None
                ):
                    session.record_compaction(
                        reactive_compaction.boundary
                    )
                state.tool_context.clear_tool_caches()
                next_state = replace(
                    state,
                    input_tokens=(
                        state.input_tokens + reactive_compaction.input_tokens
                    ),
                    output_tokens=(
                        state.output_tokens + reactive_compaction.output_tokens
                    ),
                    reactive_compaction_attempted=True,
                    transition="reactive_context_compacted",
                )
                return Continue(next_state, "reactive_context_compacted")
            error = ModelCallError(
                reactive_compaction.message or "上下文应急压缩失败",
                "prompt_too_long",
            )

        # 9. 不可恢复错误或应急重试耗尽后形成明确 Error Terminal。
        yield TextDeltaEvent(f"\n\n错误：{error}")
        return _failure_stop(state, str(error))

    # 10. 完整响应进入历史，并计算下一轮继承的资源用量。
    response_input_tokens, response_output_tokens = state.usage_values(
        getattr(response, "usage", None)
    )
    tool_calls = [item for item in response.output if item.type == "function_call"]
    if session is not None:
        session.record_runtime_event(
            "model_response_completed",
            {
                "mode": state.mode,
                "model_round": state.model_rounds + 1,
                "usage": getattr(response, "usage", None),
                "output_text": response.output_text,
                "tool_call_count": len(tool_calls),
            },
        )
    state.context_manager.append_messages(
        response.output, completes_round=not tool_calls
    )
    if session is not None:
        session.record_context_messages(
            response.output,
            completes_round=not tool_calls,
            source="model_response",
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

    # 11. 完整响应提交后优先处理 Pause/Cancel，不能继续启动新工具。
    if controller.interrupted and not tool_calls:
        return _control_stop(response_state, active_control, session, "model_completed")

    # 12. 收尾模式收到有效响应后直接形成限制 Terminal。
    if state.mode == "finalizing":
        plan = state.tool_context.plan_store.get_plan()
        if plan is not None and plan.status == "completed":
            state.tool_context.plan_store.clear_plan()
            if session is not None:
                session.record_plan_state(
                    None,
                    action="cleared",
                    reason="completed",
                )
        return Stop(
            AgentLoopResult(
                response.output_text,
                state.elapsed_seconds,
                state.stop_reason,
                "limit_reached",
            ),
            response_state,
        )

    # 13. 无工具轮次是完整边界，先应用 FIFO Steering，再审查 Plan 退出。
    if not tool_calls:
        steering_transition = _continue_with_steering(
            response_state,
            active_control,
            session,
            config,
        )
        if steering_transition is not None:
            return steering_transition
        return _apply_plan_exit(
            response_state,
            limits,
            response.output_text,
            session,
        )

    # 14. 执行工具批次；Pause/Cancel 后仍需先提交完整配对结果。
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
        session_store=session.store if session is not None else None,
        parallel_limits=config.tool_parallel,
        retry_limits=config.tool_retry,
        permission_context=(
            session.permission_context
            if session is not None
            else PermissionContext(config.permission.default_mode)
        ),
        interrupt_controller=controller,
    )
    state.context_manager.append_messages(batch.outputs, completes_round=True)
    if session is not None:
        session.record_context_messages(
            batch.outputs,
            completes_round=True,
            source="tool_results",
        )
    if controller.interrupted:
        return _control_stop(response_state, active_control, session, "tool_batch")

    # 15. 工具结果已完整提交，构造下一轮 State。
    next_state = LoopState(
        context_manager=state.context_manager,
        tool_context=state.tool_context,
        relevant_context=state.relevant_context,
        model_rounds=model_rounds,
        tool_calls=state.tool_calls + batch.budgeted_call_count,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        plan_completion_nudges=state.plan_completion_nudges,
        reactive_compaction_attempted=state.reactive_compaction_attempted,
        mode="working",
        transition="tool_results_ready",
        started_at=state.started_at,
        stop_reason=None,
    )
    steering_transition = _continue_with_steering(
        next_state,
        active_control,
        session,
        config,
    )
    if steering_transition is not None:
        return steering_transition
    return Continue(next_state, "tool_results_ready")
