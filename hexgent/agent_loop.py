"""模型调用与工具执行的核心循环。"""

from dataclasses import dataclass
from typing import Any

from .client import get_client, get_model
from .loop_control import (
    LoopLimits,
    LoopState,
    LoopStopReason,
    check_working_limit,
    stop_reason_message,
)
from .prompt import SYSTEM_PROMPT, TOOLS
from .task import InMemoryTaskStore, format_task_context, task_needs_continuation
from .tool.tool_context import ToolExecutionContext
from hexgent.tool.tool_execution import execute_tool_call


MAX_TASK_COMPLETION_NUDGES = 1


@dataclass(frozen=True)
class AgentLoopResult:
    """一次 Agent Loop 返回给界面层的结果。"""

    output_text: str
    elapsed_seconds: float
    stop_reason: LoopStopReason | None = None


def _build_instructions(context: ToolExecutionContext) -> str:
    """组合系统提示词和当前 Task 运行时状态。"""
    task_context = format_task_context(context.task_store.get_task())
    if not task_context:
        return SYSTEM_PROMPT
    return f"{SYSTEM_PROMPT}\n\n{task_context}"


def _budget_tool_result(call_id: str, reason: LoopStopReason) -> dict[str, str]:
    """为因预算不足而未执行的工具调用创建配对结果。"""
    return {
        "type": "function_call_output",
        "call_id": call_id,
        "output": f"错误：{stop_reason_message(reason)}，本次工具调用未执行",
    }


def _finalize(
    messages: list[Any],
    context: ToolExecutionContext,
    state: LoopState,
    limits: LoopLimits,
) -> AgentLoopResult:
    """在不再提供工具的情况下生成一次最终总结。"""
    # 1. 将停止原因和收尾要求加入模型上下文。
    reason = state.stop_reason or "max_model_rounds"
    messages.append(
        {
            "role": "developer",
            "content": (
                f"{stop_reason_message(reason)}。现在进入本轮收尾阶段，不得继续调用工具。"
                "请简洁总结已经完成的工作、尚未完成的工作以及建议的下一步。"
                "不要因为本轮预算耗尽而把未完成的 Task 标记为 completed 或 blocked。"
            ),
        }
    )

    # 2. 额外调用一次不带工具的模型响应，保证用户获得可理解的结束说明。
    response = get_client().responses.create(
        model=get_model(),
        instructions=_build_instructions(context),
        input=messages,
        max_output_tokens=limits.finalization_output_tokens,
    )
    messages.extend(response.output)
    state.record_usage(getattr(response, "usage", None))

    # 3. 最终答复生成后释放已完成 Task，其他状态留给下一次用户交互继续。
    task = context.task_store.get_task()
    if task is not None and task.status == "completed":
        context.task_store.clear_task()
    return AgentLoopResult(response.output_text, state.elapsed_seconds, reason)


def agent_loop(
    messages: list[Any],
    context: ToolExecutionContext | None = None,
    limits: LoopLimits | None = None,
) -> AgentLoopResult:
    """在执行预算内持续调用模型和工具，并返回最终界面结果。"""
    # 1. 初始化模型客户端、工具上下文、执行限制和本轮状态。
    client = get_client()
    execution_context = context or ToolExecutionContext(InMemoryTaskStore())
    loop_limits = limits or LoopLimits()
    state = LoopState()
    completion_nudges = 0

    while True:
        # 2. 每次正常模型调用前检查全局预算，命中后进入唯一一次收尾轮。
        stop_reason = check_working_limit(state, loop_limits)
        if stop_reason is not None:
            state.stop_reason = stop_reason
            return _finalize(messages, execution_context, state, loop_limits)

        # 3. 把最新 Task 注入系统指令，调用模型并累计轮次及 Token。
        remaining_tokens = loop_limits.max_total_tokens - state.total_tokens
        round_output_tokens = max(
            1,
            min(loop_limits.max_output_tokens_per_round, remaining_tokens),
        )
        response = client.responses.create(
            model=get_model(),
            instructions=_build_instructions(execution_context),
            input=messages,
            tools=TOOLS,
            max_output_tokens=round_output_tokens,
        )
        state.model_rounds += 1
        state.record_usage(getattr(response, "usage", None))
        messages.extend(response.output)

        # 4. 没有工具调用时，检查 Task 状态并决定继续提醒或正常返回。
        tool_calls = [
            item for item in response.output if item.type == "function_call"
        ]
        if not tool_calls:
            task = execution_context.task_store.get_task()
            if (
                task_needs_continuation(task)
                and completion_nudges < MAX_TASK_COMPLETION_NUDGES
            ):
                completion_nudges += 1
                messages.append(
                    {
                        "role": "developer",
                        "content": (
                            "当前 Task 仍处于待处理或执行中。请继续完成必要工作，或调用 "
                            "update_task_plan 准确更新整体状态后再结束本轮任务。"
                        ),
                    }
                )
                continue
            if task is not None and task.status == "completed":
                execution_context.task_store.clear_task()
            return AgentLoopResult(response.output_text, state.elapsed_seconds)

        # 5. 逐个处理工具调用；达到工具或时间上限后仍为剩余调用生成配对结果。
        results = []
        for tool_call in tool_calls:
            tool_stop_reason: LoopStopReason | None = None
            if state.tool_calls >= loop_limits.max_tool_calls:
                tool_stop_reason = "max_tool_calls"
            elif state.elapsed_seconds >= loop_limits.max_elapsed_seconds:
                tool_stop_reason = "max_elapsed_seconds"

            if tool_stop_reason is not None:
                state.stop_reason = tool_stop_reason
                results.append(_budget_tool_result(tool_call.call_id, tool_stop_reason))
                continue

            result = execute_tool_call(
                tool_call.name,
                tool_call.arguments,
                context=execution_context,
                execution_notifier=lambda name: print(
                    f"\033[33m使用工具：{name}\033[0m"
                ),
            )
            if result.status in {"success", "error"}:
                state.tool_calls += 1
            results.append(
                {
                    "type": "function_call_output",
                    "call_id": tool_call.call_id,
                    "output": result.output,
                }
            )

        # 6. 将完整工具结果加入历史；批次中触发限制时直接进入收尾轮。
        messages.extend(results)
        if state.stop_reason is not None:
            return _finalize(messages, execution_context, state, loop_limits)
