"""工具自动重试的配置与安全决策。"""

from __future__ import annotations

from dataclasses import dataclass

from .tool_result import ToolFailure


@dataclass(frozen=True)
class ToolRetryLimits:
    """一次工具调用允许使用的自动重试配置。"""

    # 是否启用工具执行阶段的自动重试；默认关闭，必须由用户显式开启。
    enabled: bool = False

    # 包含首次执行在内的最大尝试次数。
    max_attempts: int = 3

    # 第一次重试前等待的秒数。
    initial_backoff_seconds: float = 0.25

    # 每次重试后等待时间使用的增长倍数。
    backoff_multiplier: float = 2.0

    # 单次重试等待时间允许达到的最大秒数。
    max_backoff_seconds: float = 2.0


def should_retry_tool_failure(
    failure: ToolFailure | None,
    *,
    automatic_retry_safe: bool,
    attempt_count: int,
    limits: ToolRetryLimits,
) -> bool:
    """判断失败是否同时满足语义、安全性和次数边界。"""
    # 1. 配置关闭、次数耗尽或工具未声明安全时禁止自动重试。
    if (
        not limits.enabled
        or attempt_count >= limits.max_attempts
        or not automatic_retry_safe
    ):
        return False

    # 2. 只有处理器明确标记可重试的失败才进入恢复路径。
    if failure is None or not failure.retryable:
        return False

    # 3. 已提交或无法确定的副作用不能通过重复执行来恢复。
    return failure.side_effect_state in {"none", "not_started"}


def retry_backoff_seconds(
    attempt_count: int,
    limits: ToolRetryLimits,
) -> float:
    """计算当前失败之后、下一次尝试之前的退避时间。"""
    delay = limits.initial_backoff_seconds * (
        limits.backoff_multiplier ** max(0, attempt_count - 1)
    )
    return min(delay, limits.max_backoff_seconds)
