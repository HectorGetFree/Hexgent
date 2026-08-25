"""Agent Loop 状态机的公开流式入口。"""

from collections.abc import Generator
from pathlib import Path
from typing import Any

from ..client import get_client
from ..context import ContextLimits, ContextManager
from ..plan import InMemoryPlanStore
from ..session import Session
from ..tool.tool_context import ToolExecutionContext
from ..tool.tool_execution import UserConfirmation, unavailable_user_confirmation
from hexgent.events import AgentEvent, LoopTransitionEvent, RunFinishedEvent
from .limits import LoopLimits
from .loop_state import LoopState
from .transitions import Continue
from .agent_turn import run_turn


def agent_loop(
    context_manager: list[Any] | ContextManager,
    context: ToolExecutionContext | None = None,
    limits: LoopLimits | None = None,
    user_confirmation: UserConfirmation = unavailable_user_confirmation,
    context_limits: ContextLimits | None = None,
    session: Session | None = None,
) -> Generator[AgentEvent, None, None]:
    """持续驱动单轮状态迁移，并在唯一出口发送最终事件。"""
    # 1. 初始化不可变的循环配置和第一轮完整 State。
    client = get_client()
    execution_context = context or ToolExecutionContext(
        InMemoryPlanStore(),
        Path.cwd().resolve(),
    )
    execution_context.clear_tool_call_cache()
    loop_limits = limits or LoopLimits()
    active_context = (
        context_manager
        if isinstance(context_manager, ContextManager)
        else ContextManager.from_messages(context_manager)
    )
    state = LoopState(
        context_manager=active_context,
        tool_context=execution_context,
    )
    active_context_limits = context_limits or ContextLimits()
    if session is not None:
        session.start_run(
            limits_enabled=loop_limits.enforce_run_limits,
            plan=execution_context.plan_store.get_plan(),
        )

    while True:
        # 2. 每轮只产生 Continue 或 Stop，yield 事件不参与控制流判断。
        turn_output = yield from run_turn(
            state,
            client,
            loop_limits,
            user_confirmation,
            active_context_limits,
            session,
        )
        if isinstance(turn_output, Continue):
            yield LoopTransitionEvent(turn_output.reason)
            state = turn_output.next_state
            continue

        # 3. Stop 是 Agent Loop 的唯一正常出口。
        if session is not None:
            session.finish_run(
                status=turn_output.terminal.status,
                stop_reason=turn_output.terminal.stop_reason,
                elapsed_seconds=turn_output.terminal.elapsed_seconds,
                output_text=turn_output.terminal.output_text,
                plan=execution_context.plan_store.get_plan(),
            )
        yield RunFinishedEvent(turn_output.terminal)
        return
