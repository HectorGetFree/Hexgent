"""Runtime Command 的稳定协议对象。"""

from dataclasses import dataclass
from typing import Any, Literal

CommandType = Literal[
    "StartRun", "ContinueRun", "PauseRun", "ResumeRun", "SteerRun", "CancelRun"
]
Decision = Literal["ACCEPTED", "REJECTED"]


@dataclass(frozen=True)
class RuntimeCommand:
    """WS 发往 Hexgent Runtime 的业务意图。"""

    command_id: str
    command_type: CommandType
    run_id: str
    conversation_id: str
    issued_at: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class CommandDecision:
    """已经写入 Transcript 的 Command Decision。"""

    command_id: str
    run_id: str
    status: Decision
    reason_code: str | None = None
    message: str | None = None
