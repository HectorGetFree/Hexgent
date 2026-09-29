"""把 Transcript 生命周期事实投影为稳定的 Workspace 事件。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class LifecycleEvent:
    """不暴露 Hexgent Core Event 的 Workspace 生命周期事件。"""

    event_id: str
    sequence: int
    workspace_run_id: str
    conversation_id: str
    type: str
    occurred_at: str
    caused_by_command_id: str | None
    payload: dict[str, Any]


class LifecycleProjector:
    """从同一份 Transcript 生成可重复、可重放的生命周期事件。"""

    def project(self, session_id: str, records: list[dict[str, Any]]) -> list[LifecycleEvent]:
        commands: list[dict[str, Any]] = []
        result: list[LifecycleEvent] = []
        counters: dict[str, int] = {}
        latest_command: dict[str, dict[str, Any]] = {}
        for record in records:
            payload = record.get("payload") or {}
            record_type = record.get("type")
            if (
                record_type == "runtime_command_decided"
                and payload.get("decision") == "ACCEPTED"
            ):
                latest_command[payload.get("workspace_run_id", "")] = payload
                commands.append(payload)
                continue
            if record_type not in {"agent_run_started", "agent_run_finished"}:
                continue
            workspace_run_id = payload.get("workspace_run_id")
            conversation_id = payload.get("conversation_id")
            if not workspace_run_id or not conversation_id:
                continue
            event_type = self._event_type(
                record_type,
                payload,
                latest_command.get(workspace_run_id),
            )
            if event_type is None:
                continue
            counters[workspace_run_id] = counters.get(workspace_run_id, 0) + 1
            event_id = hashlib.sha256(
                f"{session_id}:{record.get('sequence')}:{event_type}".encode()
            ).hexdigest()
            result.append(
                LifecycleEvent(
                    event_id=event_id,
                    sequence=counters[workspace_run_id],
                    workspace_run_id=workspace_run_id,
                    conversation_id=conversation_id,
                    type=event_type,
                    occurred_at=record.get("created_at")
                    or datetime.utcnow().isoformat(),
                    caused_by_command_id=(
                        latest_command.get(workspace_run_id) or {}
                    ).get("command_id"),
                    payload={
                        "internalRunId": payload.get("run_id"),
                        "status": payload.get("status"),
                        "stopReason": payload.get("stop_reason"),
                        "failureCode": self._failure_code(payload),
                        "failureMessage": None,
                    },
                )
            )
        return result

    @staticmethod
    def _event_type(
        record_type: str,
        payload: dict[str, Any],
        command: dict[str, Any] | None,
    ) -> str | None:
        if record_type == "agent_run_started":
            command_type = (command or {}).get("command_type")
            return {"ContinueRun": "AgentRunContinued", "ResumeRun": "AgentRunResumed"}.get(
                command_type, "AgentRunStarted"
            )
        status = payload.get("status")
        reason = payload.get("stop_reason")
        if status == "completed":
            return "AgentRunSucceeded"
        if status == "interrupted" and reason == "user_pause":
            return "AgentRunPaused"
        if status == "incomplete" and reason == "plan_still_active":
            return "AgentRunIncomplete"
        if status == "cancelled":
            return "AgentRunCancelled"
        if status in {"blocked", "limit_reached", "failed"}:
            return "AgentRunFailed"
        return None

    @staticmethod
    def _failure_code(payload: dict[str, Any]) -> str | None:
        status = payload.get("status")
        reason = payload.get("stop_reason")
        if status == "blocked" or reason == "plan_blocked":
            return "PLAN_BLOCKED"
        if status == "limit_reached" or reason == "limit_reached":
            return "LIMIT_REACHED"
        message = str(payload.get("output_text") or payload.get("message") or "").lower()
        if "model" in message or "stream" in message:
            return "MODEL_ERROR"
        return "RUNTIME_FAILURE"
