"""Agent Loop 跨轮次继承的运行状态。"""

from dataclasses import dataclass, field
from time import monotonic
from typing import Any, Literal

from ..tool.tool_context import ToolExecutionContext
from .limits import LoopStopReason

LoopMode = Literal["working", "finalizing"]
ContinueReason = Literal[
    "tool_results_ready",
    "plan_continuation",
    "budget_finalization",
]


@dataclass(frozen=True)
class LoopState:
    """只保存下一轮模型调用必须继承的数据。"""

    # 下一轮发送给模型的完整消息历史。
    messages: list[Any]

    # 跨轮次共享的工具上下文，包括工作区和 PlanStore。
    tool_context: ToolExecutionContext

    # 已收到完整响应的正常模型调用次数。
    model_rounds: int = 0

    # 已计入执行预算的实际工具调用次数。
    tool_calls: int = 0

    # 完整模型响应累计报告的输入 Token。
    input_tokens: int = 0

    # 完整模型响应累计报告的输出 Token。
    output_tokens: int = 0

    # Plan 未完成时已经发送的继续执行提醒次数。
    plan_completion_nudges: int = 0

    # working 表示正常工具循环，finalizing 表示无工具收尾轮。
    mode: LoopMode = "working"

    # 上一轮构造当前 State 的明确原因，第一轮为空。
    transition: ContinueReason | None = None

    # 本次 Agent Run 的开始时间，用于计算总耗时。
    started_at: float = field(default_factory=monotonic)

    # 进入 finalizing 时保存的预算停止原因。
    stop_reason: LoopStopReason | None = None

    def __post_init__(self) -> None:
        """校验工作模式和停止原因之间的状态不变量。"""
        if self.mode == "working" and self.stop_reason is not None:
            raise ValueError("working State 不能包含 stop_reason")
        if self.mode == "finalizing" and self.stop_reason is None:
            raise ValueError("finalizing State 必须包含 stop_reason")
        if self.transition == "budget_finalization" and self.mode != "finalizing":
            raise ValueError("budget_finalization 必须进入 finalizing 模式")

    @property
    def total_tokens(self) -> int:
        """返回本次运行累计使用的 Token。"""
        return self.input_tokens + self.output_tokens

    @property
    def elapsed_seconds(self) -> float:
        """返回本次运行已经持续的秒数。"""
        return monotonic() - self.started_at

    def usage_values(self, usage: Any) -> tuple[int, int]:
        """将对象或字典形式的模型用量转换为 Token 数量。"""
        if usage is None:
            return 0, 0
        if isinstance(usage, dict):
            input_tokens = usage.get("input_tokens", 0)
            output_tokens = usage.get("output_tokens", 0)
        else:
            input_tokens = getattr(usage, "input_tokens", 0)
            output_tokens = getattr(usage, "output_tokens", 0)
        return input_tokens or 0, output_tokens or 0
