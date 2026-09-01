"""把 Agent Core 事件转换为稳定的网络事件协议。"""

from dataclasses import asdict
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from ...events import (
    AgentEvent,
    ContextCompactionFailedEvent,
    ContextCompactionFinishedEvent,
    ContextCompactionStartedEvent,
    LoopLimitReachedEvent,
    LoopTransitionEvent,
    PlanCreatedEvent,
    PlanUpdatedEvent,
    RunFinishedEvent,
    TextDeltaEvent,
    ToolBatchFinishedEvent,
    ToolBatchStartedEvent,
    ToolExecutionCompletedEvent,
    ToolFinishedEvent,
    ToolStartedEvent,
)


BEIJING_TIMEZONE = ZoneInfo("Asia/Shanghai")

EVENT_TYPES: dict[type[Any], str] = {
    TextDeltaEvent: "text_delta",
    ToolStartedEvent: "tool_started",
    ToolExecutionCompletedEvent: "tool_execution_completed",
    ToolFinishedEvent: "tool_finished",
    ToolBatchStartedEvent: "tool_batch_started",
    ToolBatchFinishedEvent: "tool_batch_finished",
    PlanCreatedEvent: "plan_created",
    PlanUpdatedEvent: "plan_updated",
    LoopLimitReachedEvent: "loop_limit_reached",
    LoopTransitionEvent: "loop_transition",
    ContextCompactionStartedEvent: "context_compaction_started",
    ContextCompactionFinishedEvent: "context_compaction_finished",
    ContextCompactionFailedEvent: "context_compaction_failed",
    RunFinishedEvent: "run_finished",
}


def serialize_agent_event(
    event: AgentEvent,
    *,
    run_id: str,
    sequence: int,
) -> dict[str, Any]:
    """将一个 Core 事件转换为带顺序和时间的网络事件。"""
    event_type = EVENT_TYPES.get(type(event))
    if event_type is None:
        raise ValueError(f"尚未注册 AgentEvent 的网络类型：{type(event).__name__}")
    return build_web_event(
        event_type,
        asdict(event),
        run_id=run_id,
        sequence=sequence,
    )


def build_web_event(
    event_type: str,
    payload: dict[str, Any],
    *,
    run_id: str,
    sequence: int,
) -> dict[str, Any]:
    """构造 Adapter 自身产生的稳定网络事件。"""
    return {
        "sequence": sequence,
        "type": event_type,
        "created_at": datetime.now(BEIJING_TIMEZONE).isoformat(),
        "run_id": run_id,
        "payload": payload,
    }
