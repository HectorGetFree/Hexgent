"""将较早的完整模型轮次压缩为连续性摘要。"""

from collections.abc import Generator
from dataclasses import dataclass
from typing import Any, Literal

from hexgent.events import (
    AgentEvent,
    ContextCompactionFailedEvent,
    ContextCompactionFinishedEvent,
    ContextCompactionStartedEvent,
)

from hexgent.utils.token_estimator import estimate_request_tokens, estimate_value_tokens
from .context_budget import ContextLimits
from .context import ConversationContext


COMPACTION_INSTRUCTIONS = """你负责压缩 Agent 的历史会话上下文。只输出连续性摘要，不得调用工具。
摘要必须保留：用户目标与约束、已确认决策、已完成工作、重要文件与结果、错误及修复、
未完成工作和下一步。不要把已过期的 Plan 快照当作真实状态，也不要大段复制工具输出。"""

CompactionStatus = Literal["not_needed", "compacted", "failed", "blocked"]


@dataclass(frozen=True)
class CompactionOutcome:
    """一次主动压缩检查产生的结构化结果。"""

    # 本次检查最终产生的压缩状态。
    status: CompactionStatus

    # 检查或压缩前估算的完整请求 Token 数量。
    before_tokens: int

    # 压缩完成后估算的完整请求 Token 数量。
    after_tokens: int

    # 压缩模型调用消耗的输入 Token 数量。
    input_tokens: int = 0

    # 压缩模型调用消耗的输出 Token 数量。
    output_tokens: int = 0

    # 压缩失败或无法安全继续时的中文原因。
    message: str | None = None


def _usage_values(usage: Any) -> tuple[int, int]:
    """兼容对象和字典形式的模型 Token 用量。"""
    if usage is None:
        return 0, 0
    if isinstance(usage, dict):
        return usage.get("input_tokens", 0) or 0, usage.get("output_tokens", 0) or 0
    return (
        getattr(usage, "input_tokens", 0) or 0,
        getattr(usage, "output_tokens", 0) or 0,
    )


def _select_cut_index(
    conversation: ConversationContext,
    recent_context_tokens: int,
) -> int | None:
    """选择保留近期内容且位于完整 API 轮次之后的压缩切点。"""
    eligible_boundaries = [
        boundary
        for boundary in conversation.round_boundaries
        if 0 < boundary < len(conversation.active_messages)
    ]
    for boundary in eligible_boundaries:
        recent_messages = conversation.materialize_messages(
            conversation.active_messages[boundary:]
        )
        if estimate_value_tokens(recent_messages) <= recent_context_tokens:
            return boundary
    return eligible_boundaries[-1] if eligible_boundaries else None


def _read_completed_response(stream: Any) -> Any:
    """消费内部摘要流，并只返回协议完整的 Response。"""
    completed_response = None
    try:
        for event in stream:
            if event.type == "response.completed":
                completed_response = event.response
            elif event.type in {"response.failed", "response.incomplete", "error"}:
                raise RuntimeError("上下文摘要模型没有返回完整结果")
    finally:
        close = getattr(stream, "close", None)
        if callable(close):
            close()
    if completed_response is None:
        raise RuntimeError("上下文摘要模型没有返回 response.completed 事件")
    return completed_response


def compact_context_if_needed(
    conversation: ConversationContext,
    client: Any,
    model: str,
    instructions: str,
    tools: list[dict[str, Any]],
    limits: ContextLimits,
) -> Generator[AgentEvent, None, CompactionOutcome]:
    """在主模型调用前检查容量，并在需要时压缩较早的完整轮次。"""
    # 1. 估算包含运行时指令和工具定义的完整请求大小。
    before_tokens = estimate_request_tokens(
        instructions,
        conversation.model_messages(),
        tools,
    )
    if before_tokens < limits.compaction_threshold_tokens:
        return CompactionOutcome("not_needed", before_tokens, before_tokens)

    # 2. 连续失败达到上限时停止重复调用摘要模型。
    if (
        conversation.consecutive_compaction_failures
        >= limits.max_compaction_attempts
    ):
        message = "上下文压缩已连续失败，已停止重复尝试"
        yield ContextCompactionFailedEvent(message)
        status: CompactionStatus = (
            "blocked"
            if before_tokens >= limits.blocking_threshold_tokens
            else "failed"
        )
        return CompactionOutcome(status, before_tokens, before_tokens, message=message)

    # 3. 只在完整 API 轮次边界上选择压缩范围。
    cut_index = _select_cut_index(conversation, limits.recent_context_tokens)
    if cut_index is None:
        message = "当前上下文没有可安全压缩的完整模型轮次"
        yield ContextCompactionFailedEvent(message)
        status = (
            "blocked"
            if before_tokens >= limits.blocking_threshold_tokens
            else "failed"
        )
        return CompactionOutcome(status, before_tokens, before_tokens, message=message)

    yield ContextCompactionStartedEvent(before_tokens)

    # 4. 摘要调用不暴露文字增量，也不允许使用任何工具。
    try:
        stream = client.responses.create(
            model=model,
            instructions=COMPACTION_INSTRUCTIONS,
            input=conversation.materialize_messages(
                conversation.active_messages[:cut_index]
            ),
            max_output_tokens=limits.summary_output_tokens,
            stream=True,
        )
        response = _read_completed_response(stream)
        summary = response.output_text.strip()
        if not summary:
            raise RuntimeError("上下文摘要为空")
    except Exception as error:
        conversation.record_compaction_failure()
        message = f"上下文压缩失败：{error}"
        yield ContextCompactionFailedEvent(message)
        status = (
            "blocked"
            if before_tokens >= limits.blocking_threshold_tokens
            else "failed"
        )
        return CompactionOutcome(status, before_tokens, before_tokens, message=message)

    # 5. 仅替换活动上下文，完整会话记录继续保留。
    response_input_tokens, response_output_tokens = _usage_values(
        getattr(response, "usage", None)
    )
    conversation.apply_compaction(cut_index, summary)
    after_tokens = estimate_request_tokens(
        instructions,
        conversation.model_messages(),
        tools,
    )
    yield ContextCompactionFinishedEvent(before_tokens, after_tokens)
    return CompactionOutcome(
        "compacted",
        before_tokens,
        after_tokens,
        response_input_tokens,
        response_output_tokens,
    )
