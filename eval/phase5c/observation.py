"""将 durable Transcript 投影为可归因的 Phase5C 工具诊断。"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class ToolCallDiagnostic:
    call_id: str
    tool_name: str | None
    status: str | None
    error_category: str | None
    permission_outcome: str | None
    dispatch_occurred: bool


@dataclass(frozen=True)
class RuntimeObservation:
    model_terminal_status: str | None
    stop_reason: str | None
    model_response_failures: tuple[dict[str, Any], ...]
    tool_calls: tuple[ToolCallDiagnostic, ...]

    def to_dict(self) -> dict[str, Any]:
        rows = [asdict(item) for item in self.tool_calls]
        return {
            "model_terminal_status": self.model_terminal_status,
            "stop_reason": self.stop_reason,
            "model_response_failed": list(self.model_response_failures),
            "tool_calls": rows,
            "total_tool_calls": len(rows),
            "tool_calls_by_name": dict(Counter(item["tool_name"] or "unknown" for item in rows)),
            "denied_calls": sum(item["status"] == "denied" for item in rows),
            "failed_calls": sum(item["status"] in {"error", "invalid"} for item in rows),
        }


def project_transcript(records: list[dict[str, Any]]) -> RuntimeObservation:
    calls: dict[str, dict[str, Any]] = {}
    terminal: dict[str, Any] | None = None
    failures: list[dict[str, Any]] = []
    for record in records:
        payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
        kind = record.get("type")
        call_id = payload.get("call_id") or payload.get("tool_call_id")
        if kind == "agent_run_finished":
            terminal = payload
        elif kind == "model_response_failed":
            failures.append({"reason": payload.get("reason"), "message": payload.get("message")})
        if not isinstance(call_id, str):
            continue
        call = calls.setdefault(call_id, {"tool_name": None, "status": None, "error_category": None, "permission_outcome": None, "dispatch_occurred": False})
        if isinstance(payload.get("tool_name"), str):
            call["tool_name"] = payload["tool_name"]
        if kind == "permission_resolved":
            call["permission_outcome"] = payload.get("behavior")
        elif kind == "tool_execution_dispatched":
            call["dispatch_occurred"] = True
        elif kind == "tool_execution_finished":
            call["status"] = payload.get("status")
            failure = payload.get("failure")
            if isinstance(failure, dict):
                call["error_category"] = failure.get("code")
    diagnostics = tuple(ToolCallDiagnostic(call_id, **data) for call_id, data in sorted(calls.items()))
    return RuntimeObservation(
        terminal.get("status") if terminal else None,
        terminal.get("stop_reason") if terminal else None,
        tuple(failures), diagnostics,
    )
