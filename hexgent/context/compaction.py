"""将较早的完整模型轮次压缩为连续性摘要。"""

from collections.abc import Generator
from dataclasses import dataclass
import re
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


COMPACTION_INSTRUCTIONS = """你负责压缩 Agent 的历史会话上下文。
CRITICAL：只输出文本，不得调用任何工具，不得生成新的工具调用或最终用户答复。
请先在内部分析历史，再只输出一个 <summary>...</summary> 区块。摘要必须简短并严格使用以下结构：
<summary>
当前目标：
用户约束：
已完成：
重要发现：
当前状态：
剩余工作：
错误与恢复：
</summary>
只保留继续执行所必需的事实。不要复制完整工具输出、工具协议、文件全文、旧最终报告或过期 Plan 快照。
不要把摘要生成描述成任务中断、执行权限受限或 Agent 无法继续；摘要完成后主 Agent 会自动恢复原任务。
REMINDER：只允许输出 <summary>...</summary> 文本，不得调用工具。"""

CompactionStatus = Literal[
    "not_needed",  # 当前上下文未达到主动压缩阈值，无需处理。
    "compacted",  # 压缩有效，且活动上下文已经回落到主动阈值以下。
    "partial",  # 压缩有效，但活动上下文仍高于主动阈值，允许有限次数续压缩。
    "failed",  # 本次压缩失败，但上下文尚未达到必须停止运行的硬阈值。
    "blocked",  # 上下文达到硬阈值且无法安全压缩，必须阻止后续模型调用。
]


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
        if 0 < boundary <= len(conversation.active_messages)
    ]
    for boundary in eligible_boundaries:
        recent_messages = conversation.materialize_messages(
            conversation.active_messages[boundary:]
        )
        if estimate_value_tokens(recent_messages) <= recent_context_tokens:
            return boundary
    return eligible_boundaries[-1] if eligible_boundaries else None


def _extract_summary(output_text: str) -> str | None:
    """只接受唯一且非空的结构化 summary 区块。"""
    matches = re.findall(
        r"<summary>\s*(.*?)\s*</summary>",
        output_text,
        flags=re.DOTALL,
    )
    if len(matches) != 1:
        return None
    summary = matches[0].strip()
    return summary or None


def _has_meaningful_savings(
    before_tokens: int,
    after_tokens: int,
    limits: ContextLimits,
) -> bool:
    """判断候选摘要是否产生足够的绝对或相对收益。"""
    savings = before_tokens - after_tokens
    if savings <= 0:
        return False
    savings_ratio = savings / before_tokens
    return (
        savings >= limits.min_compaction_savings_tokens
        or savings_ratio >= limits.min_compaction_savings_ratio
    )


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
    *,
    force: bool = False,
) -> Generator[AgentEvent, None, CompactionOutcome]:
    """在主模型调用前检查容量，并在需要时压缩较早的完整轮次。"""
    # 1. 估算包含运行时指令和工具定义的完整请求大小。
    before_tokens = estimate_request_tokens(
        instructions,
        conversation.model_messages(),
        tools,
    )
    if not force and before_tokens < limits.compaction_threshold_tokens:
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

    # 4. 最多进行有限次结构化摘要尝试，失败或无收益时使用更严格提醒重试。
    total_input_tokens = 0
    total_output_tokens = 0
    failure_reason = "上下文摘要没有产生有效压缩"
    recent_messages = conversation.materialize_messages(
        conversation.active_messages[cut_index:]
    )
    recent_tokens = estimate_value_tokens(recent_messages)
    for attempt in range(1, limits.max_compaction_attempts + 1):
        retry_instruction = (
            ""
            if attempt == 1
            else "\n上一次摘要格式或压缩收益不合格。必须显著缩短内容，并严格只输出 summary 区块。"
        )
        try:
            stream = client.responses.create(
                model=model,
                instructions=COMPACTION_INSTRUCTIONS + retry_instruction,
                input=conversation.materialize_messages(
                    conversation.active_messages[:cut_index]
                ),
                max_output_tokens=limits.summary_output_tokens,
                stream=True,
            )
            response = _read_completed_response(stream)
        except Exception as error:
            failure_reason = f"摘要模型调用失败：{error}"
            continue

        response_input_tokens, response_output_tokens = _usage_values(
            getattr(response, "usage", None)
        )
        total_input_tokens += response_input_tokens
        total_output_tokens += response_output_tokens
        summary = _extract_summary(response.output_text)
        if summary is None:
            failure_reason = "摘要模型没有返回唯一且非空的 <summary> 区块"
            continue

        summary_tokens = estimate_value_tokens(summary)
        if summary_tokens > limits.summary_output_tokens:
            failure_reason = "摘要内容超过配置的摘要 Token 预算"
            continue

        candidate_messages, _, _ = conversation.build_compacted_messages(
            cut_index,
            summary,
        )
        after_tokens = estimate_request_tokens(
            instructions,
            conversation.materialize_messages(candidate_messages),
            tools,
        )
        if not _has_meaningful_savings(before_tokens, after_tokens, limits):
            failure_reason = (
                f"压缩收益不足：约 {before_tokens:,} → {after_tokens:,} Token"
            )
            continue

        status = (
            "compacted"
            if after_tokens < limits.compaction_threshold_tokens
            else "partial"
        )
        if (
            status == "partial"
            and conversation.partial_compaction_count
            >= limits.max_partial_compactions
        ):
            failure_reason = "上下文仍超过主动阈值，且部分压缩次数已达到上限"
            continue

        # 5. 只有验证收益后才提交候选上下文，完整 Transcript 继续保留。
        conversation.apply_compaction(
            cut_index,
            summary,
            before_tokens=before_tokens,
            after_tokens=after_tokens,
            summary_tokens=summary_tokens,
            recent_tokens=recent_tokens,
            status=status,
        )
        yield ContextCompactionFinishedEvent(before_tokens, after_tokens, status)
        return CompactionOutcome(
            status,
            before_tokens,
            after_tokens,
            total_input_tokens,
            total_output_tokens,
        )

    # 6. 所有尝试均无效时累计熔断计数，硬阈值以上明确阻塞。
    conversation.record_compaction_failure()
    message = f"上下文压缩失败：{failure_reason}"
    yield ContextCompactionFailedEvent(message)
    status = (
        "blocked"
        if before_tokens >= limits.blocking_threshold_tokens
        else "failed"
    )
    return CompactionOutcome(
        status,
        before_tokens,
        before_tokens,
        total_input_tokens,
        total_output_tokens,
        message,
    )
