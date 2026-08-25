"""Agent Loop 跨轮次继承的运行状态。"""

from dataclasses import dataclass, field
from time import monotonic
from typing import Any, Literal

from hexgent.context.context import ConversationContext

from ..tool.tool_context import ToolExecutionContext
from .limits import LoopStopReason

LoopMode = Literal[
    "working",  # 正常调用模型、执行工具并推进 Plan。
    "finalizing",  # 已停止正常循环，只进行一次禁用工具的收尾响应。
]
ContinueReason = Literal[
    "tool_results_ready",  # 工具批次结果已回填，继续下一轮模型调用。
    "plan_continuation",  # Plan 仍在执行中，提醒模型继续推进或更新状态。
    "context_compacted",  # 活动上下文已压缩，使用新上下文重试当前轮次。
    "reactive_context_compacted",  # API 拒绝过长请求后，应急压缩并重试一次。
    "budget_finalization",  # 命中循环预算，进入禁用工具的收尾轮次。
]


@dataclass(frozen=True)
class LoopState:
    """只保存下一轮模型调用必须继承的数据。"""

    # 下一轮发送给模型的完整消息历史。
    messages: list[Any]

    # 跨轮次共享的工具上下文，包括工作区和 PlanStore。
    tool_context: ToolExecutionContext

    # 跨用户轮次共享的完整会话记录与活动模型上下文。
    conversation: ConversationContext | None = None

    # 已收到完整响应的正常模型调用次数。
    model_rounds: int = 0

    # 已计入预算的真实执行和去重工具调用次数。
    tool_calls: int = 0

    # 完整模型响应累计报告的输入 Token。
    input_tokens: int = 0

    # 完整模型响应累计报告的输出 Token。
    output_tokens: int = 0

    # Plan 未完成时已经发送的继续执行提醒次数。
    plan_completion_nudges: int = 0

    # 本次 Agent Run 是否已经因 prompt_too_long 执行过一次应急压缩。
    reactive_compaction_attempted: bool = False

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
        conversation = self.conversation or ConversationContext.from_messages(
            self.messages
        )
        object.__setattr__(self, "conversation", conversation)
        object.__setattr__(self, "messages", conversation.active_messages)
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
