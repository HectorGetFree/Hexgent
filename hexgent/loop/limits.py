"""Agent Loop 的执行预算配置与判断。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from .loop_state import LoopState


LoopStopReason = Literal[
    "max_model_rounds",  # 正常模型调用轮数达到上限。
    "max_tool_calls",  # 实际计入预算的工具调用数量达到上限。
    "max_total_tokens",  # 完整模型响应累计 Token 达到上限。
    "max_elapsed_seconds",  # 本次 Agent Run 的总执行时间达到上限。
]


@dataclass(frozen=True)
class LoopLimits:
    """一次 Agent Loop 可以消耗的最大资源。"""

    # 正常工作模式允许调用模型的最大轮数。
    max_model_rounds: int = 100

    # 一次 Agent Loop 允许计入预算的最大工具调用数量。
    max_tool_calls: int = 300

    # 所有完整模型响应允许累计消耗的最大 Token 数量。
    max_total_tokens: int = 1_000_000

    # 单次正常模型调用允许生成的最大输出 Token 数量。
    max_output_tokens_per_round: int = 8_000

    # 一次 Agent Loop 从开始到收尾允许持续的最大秒数。
    max_elapsed_seconds: float = 3_600

    # 命中预算后专用收尾模型调用允许生成的最大 Token 数量。
    finalization_output_tokens: int = 2_000

    # 活动 Plan 在模型停止执行后允许发送的继续执行提醒次数。
    max_plan_completion_nudges: int = 1

    # 是否启用模型轮次、工具次数、累计 Token 和总运行时间限制。
    enforce_run_limits: bool = True

def check_loop_limit(
    state: LoopState,
    limits: LoopLimits,
) -> LoopStopReason | None:
    """在正常模型调用入口检查全局执行预算。"""
    if not limits.enforce_run_limits:
        return None
    if state.model_rounds >= limits.max_model_rounds:
        return "max_model_rounds"
    if state.tool_calls >= limits.max_tool_calls:
        return "max_tool_calls"
    if state.total_tokens >= limits.max_total_tokens:
        return "max_total_tokens"
    if state.elapsed_seconds >= limits.max_elapsed_seconds:
        return "max_elapsed_seconds"
    return None


def stop_reason_message(reason: LoopStopReason) -> str:
    """返回适合提供给模型和用户理解的停止原因。"""
    messages = {
        "max_model_rounds": "本轮模型调用次数已达到上限",
        "max_tool_calls": "本轮工具执行次数已达到上限",
        "max_total_tokens": "本轮累计 Token 使用量已达到上限",
        "max_elapsed_seconds": "本轮执行时间已达到上限",
    }
    return messages[reason]
