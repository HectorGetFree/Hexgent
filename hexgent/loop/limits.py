"""Agent Loop 的执行预算配置与判断。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from .state import LoopState


LoopStopReason = Literal[
    "max_model_rounds",
    "max_tool_calls",
    "max_total_tokens",
    "max_elapsed_seconds",
]


@dataclass(frozen=True)
class LoopLimits:
    """一次 Agent Loop 可以消耗的最大资源。"""

    max_model_rounds: int = 100
    max_tool_calls: int = 300
    max_total_tokens: int = 1_000_000
    max_output_tokens_per_round: int = 8_000
    max_elapsed_seconds: float = 3_600
    finalization_output_tokens: int = 2_000


def check_loop_limit(
    state: LoopState,
    limits: LoopLimits,
) -> LoopStopReason | None:
    """在正常模型调用入口检查全局执行预算。"""
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
