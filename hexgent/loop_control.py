"""Agent Loop 的执行预算与运行状态。"""

from dataclasses import dataclass, field
from time import monotonic
from typing import Any, Literal


LoopStopReason = Literal[
    "max_model_rounds",
    "max_tool_calls",
    "max_total_tokens",
    "max_elapsed_seconds",
]


@dataclass(frozen=True)
class LoopLimits:
    """一次 Agent Loop 可以消耗的最大资源。"""

    max_model_rounds: int = 12
    max_tool_calls: int = 30
    max_total_tokens: int = 60_000
    max_output_tokens_per_round: int = 4_000
    max_elapsed_seconds: float = 600
    finalization_output_tokens: int = 2_000


@dataclass
class LoopState:
    """记录一次 Agent Loop 已经消耗的资源。"""

    model_rounds: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    started_at: float = field(default_factory=monotonic)
    stop_reason: LoopStopReason | None = None

    @property
    def total_tokens(self) -> int:
        """返回本次运行累计使用的 Token。"""
        return self.input_tokens + self.output_tokens

    @property
    def elapsed_seconds(self) -> float:
        """返回本次运行已经持续的秒数。"""
        return monotonic() - self.started_at

    def record_usage(self, usage: Any) -> None:
        """兼容对象或字典形式的模型用量信息。"""
        if usage is None:
            return
        if isinstance(usage, dict):
            input_tokens = usage.get("input_tokens", 0)
            output_tokens = usage.get("output_tokens", 0)
        else:
            input_tokens = getattr(usage, "input_tokens", 0)
            output_tokens = getattr(usage, "output_tokens", 0)
        self.input_tokens += input_tokens or 0
        self.output_tokens += output_tokens or 0


def check_working_limit(
    state: LoopState,
    limits: LoopLimits,
) -> LoopStopReason | None:
    """在下一次正常模型调用前检查全局执行预算。"""
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
