"""Agent Loop 向界面层发送的流式事件。"""

from dataclasses import dataclass
from typing import Literal, TypeAlias

from hexgent.plan import Plan
from hexgent.tool.tool_execution import ToolExecutionStatus
from hexgent.loop.limits import LoopStopReason
from hexgent.loop.state import ContinueReason


ToolEventStatus = ToolExecutionStatus | Literal["skipped"]
TerminalStatus = Literal[
    "completed",
    "blocked",
    "incomplete",
    "limit_reached",
    "failed",
    "cancelled",
]
PlanStopReason = Literal["plan_blocked", "plan_still_active"]
RunStopReason: TypeAlias = LoopStopReason | PlanStopReason


@dataclass(frozen=True)
class AgentLoopResult:
    """一次 Agent Loop 返回给界面层的最终结果。"""

    # 本次运行最终提供给用户的完整文本。
    output_text: str

    # 本次运行从开始到结束的总耗时秒数。
    elapsed_seconds: float

    # 本次运行停止的具体原因；正常完成时为空。
    stop_reason: RunStopReason | None = None

    # 本次运行最终进入的终止状态。
    status: TerminalStatus = "completed"


@dataclass(frozen=True)
class TextDeltaEvent:
    """模型生成的一段文本增量。"""

    # 模型新生成并应立即展示的一段文本。
    delta: str


@dataclass(frozen=True)
class ToolStartedEvent:
    """一个工具调用开始进入执行管线。"""

    # 当前开始处理的注册工具名称。
    tool_name: str

    # 模型工具调用与结果配对使用的唯一标识。
    call_id: str

    # 适合向用户展示且不会泄露敏感参数的调用摘要。
    description: str


@dataclass(frozen=True)
class ToolFinishedEvent:
    """一个工具调用完成执行管线。"""

    # 当前完成处理的注册工具名称。
    tool_name: str

    # 模型工具调用与结果配对使用的唯一标识。
    call_id: str

    # 工具经过执行管线后产生的最终状态。
    status: ToolEventStatus


@dataclass(frozen=True)
class PlanCreatedEvent:
    """Plan 创建成功后向界面层展示的初始计划。"""

    # 已经写入 PlanStore 的初始完整 Plan。
    plan: Plan


@dataclass(frozen=True)
class PlanUpdatedEvent:
    """Plan 更新成功后向界面层展示的最新计划。"""

    # 已经写入 PlanStore 的最新版本 Plan。
    plan: Plan


@dataclass(frozen=True)
class LoopLimitReachedEvent:
    """本轮运行触发了执行限制。"""

    # 触发本次 Agent Loop 收尾的预算原因。
    reason: LoopStopReason


@dataclass(frozen=True)
class LoopTransitionEvent:
    """Agent Loop 已构造完整下一轮 State。"""

    # 当前状态机继续进入下一轮的明确原因。
    reason: ContinueReason


@dataclass(frozen=True)
class RunFinishedEvent:
    """本轮运行已经产生最终结果。"""

    # Agent Loop 唯一出口产生的最终运行结果。
    result: AgentLoopResult


AgentEvent: TypeAlias = (
    TextDeltaEvent
    | ToolStartedEvent
    | ToolFinishedEvent
    | PlanCreatedEvent
    | PlanUpdatedEvent
    | LoopLimitReachedEvent
    | LoopTransitionEvent
    | RunFinishedEvent
)
