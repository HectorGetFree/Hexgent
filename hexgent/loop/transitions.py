"""Agent Loop 单轮执行产生的控制流结果。"""

from dataclasses import dataclass
from typing import TypeAlias

from hexgent.events import AgentLoopResult
from .state import ContinueReason, LoopState


@dataclass(frozen=True)
class Continue:
    """携带完整下一轮状态继续执行。"""

    next_state: LoopState
    reason: ContinueReason

    def __post_init__(self) -> None:
        if self.next_state.transition != self.reason:
            raise ValueError("Continue 原因必须与 next State 的 transition 一致")


@dataclass(frozen=True)
class Stop:
    """携带最终结果终止本次 Agent Loop。"""

    terminal: AgentLoopResult


TurnOutcome: TypeAlias = Continue | Stop
