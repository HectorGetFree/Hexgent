"""把 WS Command 映射到现有 Hexgent Web Runtime。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from ..adapters.web.run_manager import RunManager
from ..tool.tool_permission_pipeline import PermissionMode
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
        self._restore_transcript_indexes()

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

        # Phase 1 明确不实现 Accepted-but-Unapplied 的自动 Reapply。
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
            await self.manager.pause(self._manager_run_ids[command.run_id])
        elif command.command_type == "CancelRun":
            await self.manager.cancel(self._manager_run_ids[command.run_id])
        elif command.command_type == "SteerRun":
            await self.manager.steer(
                self._manager_run_ids[command.run_id],
                str(command.payload.get("instruction", "")),
            )
        return decision

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
                "decision": decision.status,
                "reason_code": decision.reason_code,
                "message": decision.message,
                "payload": command.payload,
            },
        )
        self._decisions[command.command_id] = decision

    @staticmethod
    def _permission_mode(payload: dict[str, Any]) -> PermissionMode:
        mode = payload.get("permissionMode", "default")
        return mode if mode in {"default", "accept_edits", "plan", "dont_ask"} else "default"
