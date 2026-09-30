"""把 WS Command 映射到现有 Hexgent Web Runtime。"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from typing import Any

from ..adapters.web.run_manager import RunManager
from ..tool.tool_permission_pipeline import PermissionMode
from .lifecycle import LifecycleProjector
from .protocol import CommandDecision, RuntimeCommand


@dataclass
class _WorkspaceRunAssociation:
    """由 Transcript 事实恢复的 WS logical run 与 Runtime Session 关联。"""

    workspace_run_id: str
    conversation_id: str
    session_id: str
    latest_internal_run_id: str | None = None
    continuable: bool = False


class RuntimeCommandAdapter:
    """Phase 1 Runtime Adapter；不改变 AgentEngine 内部 run_id 语义。"""

    def __init__(self, manager: RunManager) -> None:
        self.manager = manager
        self._decisions: dict[str, CommandDecision] = {}
        self._conversation_sessions: dict[str, str] = {}
        self._run_associations: dict[str, _WorkspaceRunAssociation] = {}
        self._manager_run_ids: dict[str, str] = {}
        self._command_records: dict[str, dict[str, Any]] = {}
        self._reconcile_locks: dict[str, asyncio.Lock] = {}
        self._lifecycle_projector = LifecycleProjector()
        self._restore_transcript_indexes()

    def get_command_state(self, command_id: str) -> dict[str, Any] | None:
        """从 durable Transcript 查询 Command Decision 及其因果 Effect。"""
        for session_id, records in self._all_session_records():
            decision_record = next(
                (
                    record for record in records
                    if record.get("type") == "runtime_command_decided"
                    and (record.get("payload") or {}).get("command_id") == command_id
                ),
                None,
            )
            if decision_record is None:
                continue
            payload = decision_record.get("payload") or {}
            decision = payload.get("decision")
            effect = "NONE"
            if decision == "ACCEPTED":
                effect = (
                    "APPLIED"
                    if any(
                        record.get("type") == "runtime_control_effect"
                        and (record.get("payload") or {}).get("command_id") == command_id
                        and (record.get("payload") or {}).get("effect") in {
                            "paused", "cancelled", "steering_applied",
                        }
                        for record in records
                    )
                    else "NONE"
                )
                events = self._lifecycle_projector.project(session_id, records)
                effect = "APPLIED" if effect == "APPLIED" or (
                    any(event.caused_by_command_id == command_id for event in events)
                ) else "NONE"
            return {
                "commandId": command_id,
                "integrationRunId": payload.get("workspace_run_id"),
                "type": payload.get("command_type"),
                "decision": decision,
                "reasonCode": payload.get("reason_code"),
                "effect": effect,
            }
        return None

    def get_run_state(self, workspace_run_id: str) -> dict[str, Any] | None:
        """从 Transcript 和确定性生命周期投影查询 Runtime 状态。"""
        for session_id, records in self._all_session_records():
            events = self._lifecycle_projector.project(session_id, records)
            matching = [
                event for event in events
                if event.workspace_run_id == workspace_run_id
            ]
            if not matching:
                continue
            latest = matching[-1]
            state = {
                "AgentRunStarted": "RUNNING",
                "AgentRunResumed": "RUNNING",
                "AgentRunContinued": "RUNNING",
                "AgentRunPaused": "PAUSED",
                "AgentRunIncomplete": "INCOMPLETE",
                "AgentRunSucceeded": "SUCCEEDED",
                "AgentRunFailed": "FAILED",
                "AgentRunCancelled": "CANCELLED",
            }[latest.type]
            return {
                "integrationRunId": workspace_run_id,
                "conversationId": latest.conversation_id,
                "state": state,
                "continuable": state in {"PAUSED", "INCOMPLETE"},
                "lastLifecycleSequence": latest.sequence,
            }
        return None

    def get_lifecycle_events(
        self, workspace_run_id: str, after_sequence: int = 0,
    ) -> list[dict[str, Any]]:
        """从同一 LifecycleProjector 读取指定 Workspace Run 的事件。"""
        for session_id, records in self._all_session_records():
            events = self._lifecycle_projector.project(session_id, records)
            matching = [
                {
                    "eventId": event.event_id,
                    "sequence": event.sequence,
                    "workspaceRunId": event.workspace_run_id,
                    "conversationId": event.conversation_id,
                    "type": event.type,
                    "occurredAt": event.occurred_at,
                    "causedByCommandId": event.caused_by_command_id,
                    "payload": event.payload,
                }
                for event in events
                if event.workspace_run_id == workspace_run_id
                and event.sequence > after_sequence
            ]
            if matching:
                return matching
        return []

    def _all_session_records(self) -> list[tuple[str, list[dict[str, Any]]]]:
        """读取当前工作目录下所有可解析 Transcript；它是只读恢复索引，不是新状态库。"""
        sessions_root = self.manager.workdir / ".hexgent" / "sessions"
        result: list[tuple[str, list[dict[str, Any]]]] = []
        for transcript in sessions_root.glob("*/transcript.jsonl"):
            try:
                records = [
                    json.loads(line)
                    for line in transcript.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
                result.append((transcript.parent.name, records))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
        return result

    def _restore_transcript_indexes(self) -> None:
        """启动时从 Transcript 重建 Decision 与 logical run 关联索引。"""
        sessions_root = self.manager.workdir / ".hexgent" / "sessions"
        if not sessions_root.is_dir():
            return
        for transcript in sessions_root.glob("*/transcript.jsonl"):
            try:
                session_id = transcript.parent.name
                latest_runs: dict[str, _WorkspaceRunAssociation] = {}
                for line in transcript.read_text(encoding="utf-8").splitlines():
                    record = json.loads(line)
                    payload = record.get("payload", {})
                    record_type = record.get("type")
                    if record_type == "runtime_command_decided":
                        decision = CommandDecision(
                            payload["command_id"], payload["workspace_run_id"],
                            payload["decision"], payload.get("reason_code"),
                            payload.get("message"),
                        )
                        self._decisions[decision.command_id] = decision
                        durable_command = dict(payload)
                        durable_command["sequence"] = record.get("sequence", 0)
                        self._command_records[decision.command_id] = durable_command
                        conversation_id = payload.get("conversation_id")
                        if isinstance(conversation_id, str):
                            self._conversation_sessions[conversation_id] = session_id
                    elif record_type == "agent_run_started":
                        workspace_run_id = payload.get("workspace_run_id")
                        conversation_id = payload.get("conversation_id")
                        internal_run_id = payload.get("run_id")
                        if all(isinstance(value, str) for value in (
                            workspace_run_id, conversation_id, internal_run_id,
                        )):
                            association = _WorkspaceRunAssociation(
                                workspace_run_id,
                                conversation_id,
                                session_id,
                                internal_run_id,
                            )
                            latest_runs[internal_run_id] = association
                            self._run_associations[workspace_run_id] = association
                            self._conversation_sessions[conversation_id] = session_id
                    elif record_type == "agent_run_finished":
                        internal_run_id = payload.get("run_id")
                        association = latest_runs.get(internal_run_id)
                        if association is not None:
                            association.continuable = payload.get("status") in {
                                "interrupted", "incomplete",
                            }
            except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError):
                # SessionRestorer 负责报告损坏 Session；Adapter 启动不能污染其他 Session。
                continue

    async def handle(self, command: RuntimeCommand) -> CommandDecision:
        """按 commandId 幂等处理命令，并在 Ack 前持久化 Decision。"""
        existing = self._decisions.get(command.command_id)
        if existing is not None:
            return existing

        decision = self._validate(command)
        if decision is not None:
            self._persist_rejection_if_possible(command, decision)
            return decision

        session_id = self._conversation_sessions.get(command.conversation_id)
        if command.command_type == "StartRun" and session_id is None:
            session = await self.manager.create_session(
                self._permission_mode(command.payload),
            )
            session_id = session.session_id
            self._conversation_sessions[command.conversation_id] = session_id
        else:
            assert session_id is not None
            await self.manager.restore_session(session_id)

        if command.command_type in {"ContinueRun", "ResumeRun"}:
            session = self.manager._require_session(session_id)
            if not session.engine.has_continuable_run:
                decision = CommandDecision(
                    command.command_id,
                    command.run_id,
                    "REJECTED",
                    "RUNTIME_STATE_NOT_CONTINUABLE",
                )
                self._persist_decision(session_id, command, decision)
                return decision

        decision = CommandDecision(command.command_id, command.run_id, "ACCEPTED")
        self._persist_decision(session_id, command, decision)
        self.manager._require_session(session_id).engine.session.bind_runtime_command(
            command.command_id
        )

        await self._apply_accepted_command(command, session_id)
        return decision

    async def reconcile_command_effect(self, command_id: str) -> dict[str, Any]:
        """恢复 ACCEPTED 但尚未产生 effect 的命令，不重新创建 command。"""
        lock = self._reconcile_locks.setdefault(command_id, asyncio.Lock())
        async with lock:
            state = self.get_command_state(command_id)
            if state is None or state["decision"] != "ACCEPTED":
                return state or {"commandId": command_id, "status": "NOT_FOUND"}
            if state["effect"] == "APPLIED":
                return state
            record = self._command_records.get(command_id)
            if record is None:
                return {**state, "status": "UNRECOVERABLE_PAYLOAD"}
            command = RuntimeCommand(
                command_id, record["command_type"], record["workspace_run_id"],
                record["conversation_id"], record.get("issued_at", ""),
                record.get("payload") or {},
            )
            session_id = self._conversation_sessions.get(command.conversation_id)
            if session_id is None:
                return {**state, "status": "SESSION_NOT_FOUND"}
            await self.manager.restore_session(session_id)
            self.manager._require_session(session_id).engine.session.bind_runtime_command(command_id)
            if self._is_superseded(command):
                return {**state, "status": "SUPERSEDED"}
            await self._apply_accepted_command(command, session_id)
            if command.command_type in {"StartRun", "ContinueRun", "ResumeRun"}:
                internal_run_id = self._manager_run_ids.get(command.run_id)
                if internal_run_id is not None:
                    handle = self.manager._runs.get(internal_run_id)
                    if handle is not None and handle.task is not None:
                        await asyncio.shield(handle.task)
            return self.get_command_state(command_id) or state

    async def _apply_accepted_command(self, command: RuntimeCommand, session_id: str) -> None:
        """只复用既有 Manager/Core 入口，不把恢复逻辑放进 Agent Loop。"""
        if command.command_type == "StartRun":
            response = await self.manager.start_run(
                session_id,
                str(command.payload.get("instruction", "")),
                workspace_run_id=command.run_id,
                conversation_id=command.conversation_id,
            )
            self._manager_run_ids[command.run_id] = response.run_id
        elif command.command_type in {"ContinueRun", "ResumeRun"}:
            response = await self.manager.continue_session(
                session_id,
                workspace_run_id=command.run_id,
                conversation_id=command.conversation_id,
            )
            self._manager_run_ids[command.run_id] = response.run_id
        elif command.command_type == "PauseRun":
            internal_run_id = self._manager_run_ids.get(command.run_id)
            if internal_run_id is not None:
                await self.manager.pause(internal_run_id)
            else:
                session = self.manager._require_session(session_id)
                if session.engine.has_continuable_run:
                    session.engine.session.record_runtime_control_effect(
                        command.command_id, "paused", {
                            "workspace_run_id": command.run_id,
                            "conversation_id": command.conversation_id,
                        },
                    )
        elif command.command_type == "CancelRun":
            internal_run_id = self._manager_run_ids.get(command.run_id)
            if internal_run_id is not None:
                await self.manager.cancel(internal_run_id)
            else:
                self.manager._require_session(session_id).engine.session.record_runtime_control_effect(
                    command.command_id, "cancelled", {
                        "workspace_run_id": command.run_id,
                        "conversation_id": command.conversation_id,
                    },
                )
        elif command.command_type == "SteerRun":
            internal_run_id = self._manager_run_ids.get(command.run_id)
            if internal_run_id is not None:
                await self.manager.steer(
                    internal_run_id, str(command.payload.get("instruction", "")),
                )
            else:
                # 进程重启后没有旧 Mailbox；先提交幂等 durable intent，等待下一次安全执行边界消费。
                self.manager._require_session(session_id).engine.session.record_runtime_control_effect(
                    command.command_id, "steering_intent", {
                        "workspace_run_id": command.run_id,
                        "conversation_id": command.conversation_id,
                        "instruction": str(command.payload.get("instruction", "")),
                    },
                )

    def _is_superseded(self, command: RuntimeCommand) -> bool:
        """只对尚未产生 effect 的控制命令执行 durable sequence 仲裁。"""
        if command.command_type not in {"PauseRun", "SteerRun"}:
            return False
        current = self._command_records[command.command_id].get("sequence", 0)
        return any(
            record.get("workspace_run_id") == command.run_id
            and record.get("sequence", 0) > current
            and record.get("decision") == "ACCEPTED"
            and record.get("command_type") == "CancelRun"
            for record in self._command_records.values()
        )

    def _validate(self, command: RuntimeCommand) -> CommandDecision | None:
        if not command.command_id or not command.run_id or not command.conversation_id:
            return CommandDecision(
                command.command_id,
                command.run_id,
                "REJECTED",
                "INVALID_ARGUMENT",
            )
        if command.command_type == "StartRun" and not command.payload.get("instruction"):
            return CommandDecision(
                command.command_id,
                command.run_id,
                "REJECTED",
                "INVALID_ARGUMENT",
            )
        association = self._run_associations.get(command.run_id)
        if command.command_type == "StartRun" and association is not None:
            reason = (
                "CONVERSATION_MISMATCH"
                if association.conversation_id != command.conversation_id
                else "RUN_ALREADY_EXISTS"
            )
            return CommandDecision(command.command_id, command.run_id, "REJECTED", reason)
        if command.command_type != "StartRun" and association is None:
            return CommandDecision(
                command.command_id,
                command.run_id,
                "REJECTED",
                "RUN_NOT_FOUND",
            )
        if association is not None and association.conversation_id != command.conversation_id:
            return CommandDecision(
                command.command_id,
                command.run_id,
                "REJECTED",
                "CONVERSATION_MISMATCH",
            )
        return None

    def _persist_rejection_if_possible(
        self,
        command: RuntimeCommand,
        decision: CommandDecision,
    ) -> None:
        """已有 Session 的拒绝决定同样必须先进入 durable Transcript。"""
        session_id = self._conversation_sessions.get(command.conversation_id)
        if session_id is None:
            association = self._run_associations.get(command.run_id)
            session_id = association.session_id if association is not None else None
        if session_id is not None:
            self._persist_decision(session_id, command, decision)
            return
        self._decisions[command.command_id] = decision

    def _persist_decision(
        self,
        session_id: str,
        command: RuntimeCommand,
        decision: CommandDecision,
    ) -> None:
        """先 durable append，再把 Decision 放入进程内去重索引。"""
        session = self.manager._require_session(session_id)
        session.engine.session.record_runtime_event(
            "runtime_command_decided",
            {
                "command_id": command.command_id,
                "workspace_run_id": command.run_id,
                "conversation_id": command.conversation_id,
                "command_type": command.command_type,
                "issued_at": command.issued_at,
                "decision": decision.status,
                "reason_code": decision.reason_code,
                "message": decision.message,
                "payload": command.payload,
            },
        )
        durable_command = {
            "command_id": command.command_id,
            "workspace_run_id": command.run_id,
            "conversation_id": command.conversation_id,
            "command_type": command.command_type,
            "issued_at": command.issued_at,
            "decision": decision.status,
            "payload": command.payload,
            "sequence": session.engine.session.store.next_sequence - 1,
        }
        self._command_records[command.command_id] = durable_command
        self._decisions[command.command_id] = decision

    @staticmethod
    def _permission_mode(payload: dict[str, Any]) -> PermissionMode:
        mode = payload.get("permissionMode", "default")
        if mode == "auto_approve" and os.environ.get("HEXGENT_ENABLE_AUTO_APPROVE") == "true":
            return "auto_approve"
        return mode if mode in {"default", "accept_edits", "plan", "dont_ask"} else "default"
