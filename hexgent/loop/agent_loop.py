"""Agent Loop 状态机的公开流式入口。"""

from collections.abc import Generator
from pathlib import Path
from typing import Any

from ..client import get_client
from ..task import InMemoryTaskStore
from ..tool.tool_context import ToolExecutionContext
from ..tool.tool_execution import UserConfirmation, unavailable_user_confirmation
from hexgent.events import AgentEvent, LoopTransitionEvent, RunFinishedEvent
from .limits import LoopLimits
from .state import LoopState
from .transitions import Continue
from .agent_turn import run_turn


def agent_loop(
    messages: list[Any],
    context: ToolExecutionContext | None = None,
    limits: LoopLimits | None = None,
    user_confirmation: UserConfirmation = unavailable_user_confirmation,
) -> Generator[AgentEvent, None, None]:
    """持续驱动单轮状态迁移，并在唯一出口发送最终事件。"""
    # 1. 初始化不可变的循环配置和第一轮完整 State。
    client = get_client()
    execution_context = context or ToolExecutionContext(
        InMemoryTaskStore(),
        Path.cwd().resolve(),
    )
    loop_limits = limits or LoopLimits()
    state = LoopState(messages=messages, tool_context=execution_context)

    while True:
        # 2. 每轮只产生 Continue 或 Stop，yield 事件不参与控制流判断。
        turn_output = yield from run_turn(
            state,
            client,
            loop_limits,
            user_confirmation,
        )
        if isinstance(turn_output, Continue):
            yield LoopTransitionEvent(turn_output.reason)
            state = turn_output.next_state
            continue

        # 3. Stop 是 Agent Loop 的唯一正常出口。
        yield RunFinishedEvent(turn_output.terminal)
        return
