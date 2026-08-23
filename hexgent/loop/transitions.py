"""Agent Loop 单轮执行产生的控制流结果。"""

from dataclasses import dataclass
from typing import TypeAlias

from hexgent.events import AgentLoopResult
from .loop_state import ContinueReason, LoopState


@dataclass(frozen=True)
class Continue:
    """携带完整下一轮状态继续执行。"""

    # 下一轮模型调用需要继承的完整 LoopState。
    next_state: LoopState

    # 当前轮次选择继续执行的明确原因。
    reason: ContinueReason

    def __post_init__(self) -> None:
        if self.next_state.transition != self.reason:
            raise ValueError("Continue 原因必须与 next State 的 transition 一致")


@dataclass(frozen=True)
class Stop:
    """携带最终结果终止本次 Agent Loop。"""

    # 发送给 Loop 唯一出口的终止结果。
    terminal: AgentLoopResult


TurnOutcome: TypeAlias = Continue | Stop
