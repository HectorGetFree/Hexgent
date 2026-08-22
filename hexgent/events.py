"""Agent Loop 向界面层发送的流式事件。"""

from dataclasses import dataclass
from typing import Literal, TypeAlias

from hexgent.task import Task
from hexgent.tool.tool_execution import ToolExecutionStatus
from hexgent.loop.limits import LoopStopReason
from hexgent.loop.state import ContinueReason


ToolEventStatus = ToolExecutionStatus | Literal["skipped"]
TerminalStatus = Literal["completed", "limit_reached", "failed", "cancelled"]


@dataclass(frozen=True)
class AgentLoopResult:
    """一次 Agent Loop 返回给界面层的最终结果。"""

    output_text: str
    elapsed_seconds: float
    stop_reason: LoopStopReason | None = None
    status: TerminalStatus = "completed"


@dataclass(frozen=True)
class TextDeltaEvent:
    """模型生成的一段文本增量。"""

    delta: str


@dataclass(frozen=True)
class ToolStartedEvent:
    """一个工具调用开始进入执行管线。"""

    tool_name: str
    call_id: str
    description: str


@dataclass(frozen=True)
class ToolFinishedEvent:
    """一个工具调用完成执行管线。"""

    tool_name: str
    call_id: str
    status: ToolEventStatus


@dataclass(frozen=True)
class TaskCreatedEvent:
    """Task 创建成功后向界面层展示的初始计划。"""

    task: Task


@dataclass(frozen=True)
class TaskUpdatedEvent:
    """Task 更新成功后向界面层展示的最新计划。"""

    task: Task


@dataclass(frozen=True)
class LoopLimitReachedEvent:
    """本轮运行触发了执行限制。"""

    reason: LoopStopReason


@dataclass(frozen=True)
class LoopTransitionEvent:
    """Agent Loop 已构造完整下一轮 State。"""

    reason: ContinueReason


@dataclass(frozen=True)
class RunFinishedEvent:
    """本轮运行已经产生最终结果。"""

    result: AgentLoopResult


AgentEvent: TypeAlias = (
    TextDeltaEvent
    | ToolStartedEvent
    | ToolFinishedEvent
    | TaskCreatedEvent
    | TaskUpdatedEvent
    | LoopLimitReachedEvent
    | LoopTransitionEvent
    | RunFinishedEvent
)
