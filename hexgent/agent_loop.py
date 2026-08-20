"""模型调用与工具执行的核心循环。"""

from collections.abc import Generator
from typing import Any

from .stream_events import (
    AgentEvent,
    AgentLoopResult,
    LoopLimitReachedEvent,
    RunFinishedEvent,
    TaskCreatedEvent,
    TaskUpdatedEvent,
    TextDeltaEvent,
    ToolFinishedEvent,
    ToolStartedEvent,
)
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
from hexgent.tool.tool_registry import get_tool


MAX_TASK_COMPLETION_NUDGES = 1


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


def _stream_model_response(
    client: Any,
    **request: Any,
) -> Generator[AgentEvent, None, Any]:
    """流式发送文本事件，并返回协议完整的模型响应。"""
    # 1. 发起流式请求，只把文本增量立即交给界面层。
    stream = client.responses.create(**request, stream=True)
    completed_response = None
    try:
        for event in stream:
            if event.type == "response.output_text.delta":
                yield TextDeltaEvent(event.delta)
            elif event.type == "response.completed":
                completed_response = event.response
            elif event.type == "response.failed":
                response = getattr(event, "response", None)
                error = getattr(event, "error", None) or getattr(response, "error", None)
                message = getattr(error, "message", None) or error or "未知错误"
                raise RuntimeError(f"模型流式响应失败：{message}")
            elif event.type == "response.incomplete":
                response = getattr(event, "response", None)
                details = getattr(response, "incomplete_details", None)
                reason = getattr(details, "reason", None)
                suffix = f"：{reason}" if reason else ""
                raise RuntimeError(
                    f"模型流式响应未完整结束{suffix}，已停止处理本轮工具调用"
                )
            elif event.type == "error":
                message = getattr(event, "message", None) or "未知错误"
                raise RuntimeError(f"模型流式响应错误：{message}")
    finally:
        close = getattr(stream, "close", None)
        if callable(close):
            close()

    # 2. 只有 completed 事件中的完整 Response 可以进入后续工具循环。
    if completed_response is None:
        raise RuntimeError("模型流式响应结束，但没有收到 response.completed 事件")
    return completed_response


def _finalize_stream(
    client: Any,
    messages: list[Any],
    context: ToolExecutionContext,
    state: LoopState,
    limits: LoopLimits,
) -> Generator[AgentEvent, None, AgentLoopResult]:
    """在不再提供工具的情况下流式生成一次最终总结。"""
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

    # 2. 额外发起一次不带工具的流式响应，保证用户获得可理解的结束说明。
    response = yield from _stream_model_response(
        client,
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


def agent_loop_stream(
    messages: list[Any],
    context: ToolExecutionContext | None = None,
    limits: LoopLimits | None = None,
) -> Generator[AgentEvent, None, None]:
    """在执行预算内持续产生模型、工具和最终结果事件。"""
    # 1. 初始化模型客户端、工具上下文、执行限制和本轮状态。
    client = get_client()
    execution_context = context or ToolExecutionContext(InMemoryTaskStore())
    loop_limits = limits or LoopLimits()
    state = LoopState()
    completion_nudges = 0

    while True:
        # 2. 每次正常模型调用前检查预算，命中后进入唯一一次收尾流。
        stop_reason = check_working_limit(state, loop_limits)
        if stop_reason is not None:
            state.stop_reason = stop_reason
            yield LoopLimitReachedEvent(stop_reason)
            result = yield from _finalize_stream(
                client,
                messages,
                execution_context,
                state,
                loop_limits,
            )
            yield RunFinishedEvent(result)
            return

        # 3. 流式调用模型，完成后再累计轮次、Token 并保存完整输出。
        remaining_tokens = loop_limits.max_total_tokens - state.total_tokens
        round_output_tokens = max(
            1,
            min(loop_limits.max_output_tokens_per_round, remaining_tokens),
        )
        response = yield from _stream_model_response(
            client,
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
            yield RunFinishedEvent(
                AgentLoopResult(response.output_text, state.elapsed_seconds)
            )
            return

        # 5. 逐个处理工具调用，并为开始、结束和预算事件提供实时反馈。
        results = []
        for tool_call in tool_calls:
            registered_tool = get_tool(tool_call.name)
            description = (
                registered_tool.describe(tool_call.arguments)
                if registered_tool is not None
                else f"{tool_call.name}()"
            )
            yield ToolStartedEvent(
                tool_call.name,
                tool_call.call_id,
                description,
            )

            tool_stop_reason: LoopStopReason | None = None
            if state.tool_calls >= loop_limits.max_tool_calls:
                tool_stop_reason = "max_tool_calls"
            elif state.elapsed_seconds >= loop_limits.max_elapsed_seconds:
                tool_stop_reason = "max_elapsed_seconds"

            if tool_stop_reason is not None:
                if state.stop_reason is None:
                    state.stop_reason = tool_stop_reason
                    yield LoopLimitReachedEvent(tool_stop_reason)
                results.append(_budget_tool_result(tool_call.call_id, tool_stop_reason))
                yield ToolFinishedEvent(
                    tool_call.name,
                    tool_call.call_id,
                    "skipped",
                )
                continue

            result = execute_tool_call(
                tool_call.name,
                tool_call.arguments,
                context=execution_context,
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
            yield ToolFinishedEvent(
                tool_call.name,
                tool_call.call_id,
                result.status,
            )
            if tool_call.name == "create_task" and result.status == "success":
                task = execution_context.task_store.get_task()
                if task is not None:
                    yield TaskCreatedEvent(task)
            elif tool_call.name == "update_task_plan" and result.status == "success":
                task = execution_context.task_store.get_task()
                if task is not None:
                    yield TaskUpdatedEvent(task)

        # 6. 将完整工具结果加入历史；批次中触发限制时直接进入收尾流。
        messages.extend(results)
        if state.stop_reason is not None:
            result = yield from _finalize_stream(
                client,
                messages,
                execution_context,
                state,
                loop_limits,
            )
            yield RunFinishedEvent(result)
            return


def agent_loop(
    messages: list[Any],
    context: ToolExecutionContext | None = None,
    limits: LoopLimits | None = None,
) -> AgentLoopResult:
    """消费完整事件流，并为非流式调用方返回最终结果。"""
    # 1. 运行流式核心，但不负责展示任何中间事件。
    final_result = None
    for event in agent_loop_stream(messages, context=context, limits=limits):
        if isinstance(event, RunFinishedEvent):
            final_result = event.result

    # 2. 正常事件流必须以 RunFinishedEvent 结束。
    if final_result is None:
        raise RuntimeError("Agent Loop 已结束，但没有产生最终运行结果")
    return final_result
