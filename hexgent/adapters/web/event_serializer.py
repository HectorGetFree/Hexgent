"""把 Agent Core 事件转换为稳定的网络事件协议。"""

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from ...events import (
    AgentEvent,
    ContextCompactionFailedEvent,
    ContextCompactionFinishedEvent,
    ContextCompactionStartedEvent,
    CheckpointCommittedEvent,
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
from ...plan import Plan


BEIJING_TIMEZONE = ZoneInfo("Asia/Shanghai")

STREAM_EVENT_TYPES = frozenset({
    "text_delta",
    "tool_started",
    "tool_finished",
    "plan_updated",
})


def serialize_agent_event(
    event: AgentEvent,
    *,
    run_id: str,
    stream_id: str,
    sequence: int,
    response_id: str | None = None,
) -> dict[str, Any] | None:
    """将 Core 事件转换为 Phase 3 允许公开的非权威流事件。"""
    event_type, payload = agent_event_payload(event)
    if event_type not in STREAM_EVENT_TYPES:
        return None
    if event_type == "text_delta":
        payload = {"responseId": response_id, **payload}
    return build_web_event(
        event_type,
        payload,
        run_id=run_id,
        stream_id=stream_id,
        sequence=sequence,
    )


def agent_event_payload(event: AgentEvent) -> tuple[str, dict[str, Any]]:
    """显式映射 Core 事件，避免领域对象新增字段时意外扩散到网络。"""
    if isinstance(event, TextDeltaEvent):
        return "text_delta", {"delta": event.delta}
    if isinstance(event, ToolStartedEvent):
        return "tool_started", {
            "tool_name": event.tool_name,
            "call_id": event.call_id,
            "description": event.description,
        }
    if isinstance(event, ToolExecutionCompletedEvent):
        return "tool_execution_completed", {
            "tool_name": event.tool_name,
            "call_id": event.call_id,
            "status": event.status,
        }
    if isinstance(event, ToolFinishedEvent):
        return "tool_finished", {
            "tool_name": event.tool_name,
            "call_id": event.call_id,
            "status": event.status,
        }
    if isinstance(event, ToolBatchStartedEvent):
        return "tool_batch_started", {
            "batch_id": event.batch_id,
            "tool_count": event.tool_count,
            "max_workers": event.max_workers,
        }
    if isinstance(event, ToolBatchFinishedEvent):
        return "tool_batch_finished", {
            "batch_id": event.batch_id,
            "tool_count": event.tool_count,
            "succeeded_count": event.succeeded_count,
            "failed_count": event.failed_count,
            "skipped_count": event.skipped_count,
            "elapsed_seconds": event.elapsed_seconds,
        }
    if isinstance(event, PlanCreatedEvent):
        return "plan_created", {"plan": serialize_plan(event.plan)}
    if isinstance(event, PlanUpdatedEvent):
        return "plan_updated", {"plan": serialize_plan(event.plan)}
    if isinstance(event, LoopLimitReachedEvent):
        return "loop_limit_reached", {"reason": event.reason}
    if isinstance(event, LoopTransitionEvent):
        return "loop_transition", {"reason": event.reason}
    if isinstance(event, CheckpointCommittedEvent):
        return "checkpoint_committed", {
            "checkpoint_id": event.checkpoint_id,
            "transcript_sequence": event.transcript_sequence,
            "reason": event.reason,
        }
    if isinstance(event, ContextCompactionStartedEvent):
        return "context_compaction_started", {
            "before_tokens": event.before_tokens,
        }
    if isinstance(event, ContextCompactionFinishedEvent):
        return "context_compaction_finished", {
            "before_tokens": event.before_tokens,
            "after_tokens": event.after_tokens,
            "status": event.status,
        }
    if isinstance(event, ContextCompactionFailedEvent):
        return "context_compaction_failed", {"message": event.message}
    if isinstance(event, RunFinishedEvent):
        return "run_finished", {
            "result": {
                "output_text": event.result.output_text,
                "elapsed_seconds": event.result.elapsed_seconds,
                "stop_reason": event.result.stop_reason,
                "status": event.result.status,
            }
        }
    raise TypeError(f"不支持的 AgentEvent：{type(event).__name__}")


def serialize_plan(plan: Plan) -> dict[str, Any]:
    """显式构造 Plan 网络快照。"""
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


def build_web_event(
    event_type: str,
    payload: dict[str, Any],
    *,
    run_id: str,
    stream_id: str,
    sequence: int,
) -> dict[str, Any]:
    """构造 Adapter 自身产生的稳定网络事件。"""
    return {
        "sequence": sequence,
        "type": event_type,
        "created_at": datetime.now(BEIJING_TIMEZONE).isoformat(),
        "run_id": run_id,
        "stream_id": stream_id,
        "payload": payload,
    }
