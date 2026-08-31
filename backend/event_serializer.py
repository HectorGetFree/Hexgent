"""把稳定 AgentEvent 显式映射为对外 Web 事件。"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from hexgent.events import (
    AgentEvent, ContextCompactionFailedEvent, ContextCompactionFinishedEvent,
    ContextCompactionStartedEvent, LoopLimitReachedEvent, LoopTransitionEvent,
    PlanCreatedEvent, PlanUpdatedEvent, RunFinishedEvent, TextDeltaEvent,
    ToolBatchFinishedEvent, ToolBatchStartedEvent, ToolExecutionCompletedEvent,
    ToolFinishedEvent, ToolStartedEvent,
)
from hexgent.plan import Plan


@dataclass(frozen=True)
class SerializedEvent:
    """一个带 Run 内严格递增序号的稳定 Web 事件。"""

    sequence: int
    type: str
    created_at: datetime
    run_id: str
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        """转换为可以直接进行 JSON 编码的公开字典。"""
        return {
            "sequence": self.sequence,
            "type": self.type,
            "created_at": self.created_at.isoformat(),
            "run_id": self.run_id,
            "payload": self.payload,
        }


def event_payload(event: AgentEvent) -> tuple[str, dict[str, Any]]:
    """逐类声明公开字段，新增内部字段不会自动泄露到 API。"""
    if isinstance(event, TextDeltaEvent):
        return "text_delta", {"delta": event.delta}
    if isinstance(event, ToolStartedEvent):
        return "tool_started", {"tool_name": event.tool_name, "call_id": event.call_id, "description": event.description}
    if isinstance(event, ToolExecutionCompletedEvent):
        return "tool_execution_completed", {"tool_name": event.tool_name, "call_id": event.call_id, "status": event.status}
    if isinstance(event, ToolFinishedEvent):
        return "tool_finished", {"tool_name": event.tool_name, "call_id": event.call_id, "status": event.status}
    if isinstance(event, ToolBatchStartedEvent):
        return "tool_batch_started", {"batch_id": event.batch_id, "tool_count": event.tool_count, "max_workers": event.max_workers}
    if isinstance(event, ToolBatchFinishedEvent):
        return "tool_batch_finished", {
            "batch_id": event.batch_id, "tool_count": event.tool_count,
            "succeeded_count": event.succeeded_count,
            "failed_count": event.failed_count,
            "skipped_count": event.skipped_count,
            "elapsed_seconds": event.elapsed_seconds,
        }
    if isinstance(event, PlanCreatedEvent):
        return "plan_created", {"plan": serialize_plan(event.plan)}
    if isinstance(event, PlanUpdatedEvent):
        return "plan_updated", {"plan": serialize_plan(event.plan)}
    if isinstance(event, ContextCompactionStartedEvent):
        return "context_compaction_started", {"before_tokens": event.before_tokens}
    if isinstance(event, ContextCompactionFinishedEvent):
        return "context_compaction_finished", {
            "before_tokens": event.before_tokens,
            "after_tokens": event.after_tokens,
            "status": event.status,
        }
    if isinstance(event, ContextCompactionFailedEvent):
        return "context_compaction_failed", {"message": event.message}
    if isinstance(event, LoopLimitReachedEvent):
        return "loop_limit_reached", {"reason": event.reason}
    if isinstance(event, LoopTransitionEvent):
        return "loop_transition", {"reason": event.reason}
    if isinstance(event, RunFinishedEvent):
        return "run_finished", {"result": {
            "output_text": event.result.output_text,
            "elapsed_seconds": event.result.elapsed_seconds,
            "stop_reason": event.result.stop_reason,
            "status": event.result.status,
        }}
    raise TypeError(f"不支持的 AgentEvent：{type(event).__name__}")


def serialize_plan(plan: Plan) -> dict[str, Any]:
    """显式声明 Plan 的公开快照，避免领域模型新增字段时自动扩散。"""
    return {
        "objective": plan.objective,
        "status": plan.status,
        "steps": [
            {
                "content": step.content,
                "process_message": step.process_message,
                "status": step.status,
                "step_id": step.step_id,
            }
            for step in plan.steps
        ],
        "revision": plan.revision,
        "last_replan_reason": plan.last_replan_reason,
        "blocked_reason": plan.blocked_reason,
    }
