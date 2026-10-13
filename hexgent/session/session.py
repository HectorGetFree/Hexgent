"""管理 Session 生命周期、Transcript 和会话工件。"""

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..context.context_manager import UserQuery
from ..utils.retrieval_text import is_context_reference_query
from ..memory import (
    ContextRecordInput,
    ContextRecordMutation,
    ContextRecordRetriever,
    ContextRecordStore,
    ContextRetrievalLimits,
    RetrievedContextRecord,
)
from ..plan import Plan
from ..tool.tool_permission_pipeline import PermissionContext, PermissionMode
from .run_report import build_run_report
from .session_store import SessionStore, ToolResultLimits
from .checkpoint import (
    Checkpoint,
    CheckpointReason,
    CheckpointRunStatus,
    CheckpointStore,
)
from .ownership import session_write


@dataclass(frozen=True)
class SessionSettings:
    """当前 Session 独立持有且可被执行层快照的运行设置。"""

    # 当前 Session 采用的工具权限模式。
    permission_mode: PermissionMode = "default"


@dataclass
class Session:
    """独立于模型 Context 的可持久化会话能力。"""

    # 可选的 JSONL Transcript 与会话工件存储。
    store: SessionStore | None = None

    # 当前会话实际采用的权限设置，初始化后不受全局配置变化影响。
    settings: SessionSettings = field(default_factory=SessionSettings)

    # 当前 Session 的可追溯核心上下文记录，不进入 ContextManager 活动消息。
    context_records: ContextRecordStore = field(default_factory=ContextRecordStore)

    # 已成功完成 Record 提取处理的最后一条 Transcript 序号；空结果同样推进。
    last_extracted_transcript_sequence: int = 0

    # 当前 Run 内提取已经失败时置为真，避免对同一批稳定事实同步重复超时。
    context_record_extraction_suppressed: bool = False

    # 最近一次由 Context 接受的用户原始 Query。
    _latest_user_query: str | None = field(default=None, init=False, repr=False)

    # 标识当前 Session 的权限模式是否已经写入 Transcript。
    _permission_mode_recorded: bool = field(default=False, init=False, repr=False)

    # 当前正在执行且尚未生成 Report 的运行编号。
    _active_run_id: str | None = field(default=None, init=False, repr=False)

    # 当前 Run 在 Transcript 中的起始记录序号。
    _active_run_start_sequence: int | None = field(
        default=None,
        init=False,
        repr=False,
    )

    # Integration 关联字段；不参与 internal run_id 的生成。
    _workspace_run_id: str | None = field(default=None, init=False, repr=False)
    # 最近一次 Runtime Command 的因果标识；只写入 durable lifecycle fact。
    _caused_by_command_id: str | None = field(default=None, init=False, repr=False)
    _conversation_id: str | None = field(default=None, init=False, repr=False)

    @classmethod
    def persistent(
        cls,
        workdir: Path,
        tool_result_limits: ToolResultLimits | None = None,
        settings: SessionSettings | None = None,
    ) -> "Session":
        """创建持续写入工作区 Session 目录的会话。"""
        return cls(
            SessionStore(workdir, tool_result_limits=tool_result_limits),
            settings or SessionSettings(),
        )

    @classmethod
    def restored(
        cls,
        store: SessionStore,
        settings: SessionSettings,
        context_records: ContextRecordStore | None = None,
    ) -> "Session":
        """使用已经打开的 Store 构造可继续追加事实的 Session。"""
        session = cls(store, settings, context_records or ContextRecordStore())
        session._permission_mode_recorded = True
        return session

    def restore_latest_user_query(self, content: str) -> None:
        """恢复最近 Query，供 continue 启动新的 Session 内 Run 使用。"""
        self._latest_user_query = content

    @property
    def permission_context(self) -> PermissionContext:
        """取得当前 Session 权限模式的不可变执行快照。"""
        return PermissionContext(self.settings.permission_mode)

    def record_user_query(
        self, query: UserQuery, *, source: str = "user", command_id: str | None = None,
    ) -> None:
        """先初始化 Session，再记录一条进入活动 Context 的原始 Query。"""
        self._latest_user_query = query.content
        if self.store is not None:
            self.store.start_session(query.content)
            if not self._permission_mode_recorded:
                self.store.append_transcript(
                    "session_settings",
                    {"permission_mode": self.settings.permission_mode},
                )
                self._permission_mode_recorded = True
            self.store.append_transcript(
                "context_user_query_appended",
                {
                    "query_sequence": query.sequence,
                    "content": query.content,
                    "source": source,
                    "command_id": command_id,
                },
            )

    def initialize_runtime_session(self, session_id: str) -> None:
        """为外部 Runtime Adapter 预先创建可写入命令事实的持久化 Session。"""
        if self.store is not None:
            self.store.start_named_session(session_id, "Workspace Conversation")

    def start_run(self, *, limits_enabled: bool, plan: Plan | None) -> None:
        """开始跟踪一次由最新用户 Query 驱动的 Agent Run。"""
        if self.store is None:
            return
        if self._latest_user_query is None:
            raise RuntimeError("Session 尚未记录用户 Query，不能开始 Agent Run")
        if self._active_run_id is not None:
            raise RuntimeError("当前 Session 已有尚未结束的 Agent Run")

        # 每个新的 Run 可以重新尝试未提交的提取；同一 Run 内失败后不重复阻塞主链路。
        self.context_record_extraction_suppressed = False
        self._active_run_id = self.store.next_run_id()
        self._active_run_start_sequence = self.store.next_sequence
        self.record_runtime_event(
            "agent_run_started",
            {
                "run_id": self._active_run_id,
                "workspace_run_id": self._workspace_run_id,
                "conversation_id": self._conversation_id,
                "caused_by_command_id": self._caused_by_command_id,
                "limits_enabled": limits_enabled,
                "user_query": self._latest_user_query,
                "plan": asdict(plan) if plan is not None else None,
            },
        )

    def bind_runtime_run(self, workspace_run_id: str, conversation_id: str) -> None:
        """绑定当前 internal run 的 WS 业务身份。"""
        self._workspace_run_id = workspace_run_id
        self._conversation_id = conversation_id

    def bind_runtime_command(self, command_id: str) -> None:
        """为本次执行绑定协议层 command 因果关系，不改变 internal run_id。"""
        self._caused_by_command_id = command_id

    def finish_run(
        self,
        *,
        status: str,
        stop_reason: str | None,
        elapsed_seconds: float,
        output_text: str,
        plan: Plan | None,
        context_manager: Any | None = None,
        loop_state: Any | None = None,
    ) -> Path | None:
        """结束当前 Agent Run，并由 Transcript 事实生成 JSON Report。"""
        if self.store is None:
            return None
        if self._active_run_id is None or self._active_run_start_sequence is None:
            raise RuntimeError("当前 Session 没有可以结束的 Agent Run")

        # 1. 先把终态写入事实日志，确保 Report 始终可以重新生成。
        run_id = self._active_run_id
        start_sequence = self._active_run_start_sequence
        self.record_runtime_event(
            "agent_run_finished",
            {
                "run_id": run_id,
                "workspace_run_id": self._workspace_run_id,
                "conversation_id": self._conversation_id,
                "caused_by_command_id": self._caused_by_command_id,
                "status": status,
                "stop_reason": stop_reason,
                "elapsed_seconds": elapsed_seconds,
                "output_text": output_text,
                "plan": asdict(plan) if plan is not None else None,
            },
        )

        # 2. Run 终态已经写入 Transcript 后，保存对应的安全边界快照。
        if context_manager is not None and self.store.session_id is not None:
            try:
                self.create_checkpoint(
                    context_manager=context_manager,
                    plan=plan,
                    reason="run_finished",
                    loop_state=loop_state,
                    run_status=(
                        "continuable"
                        if status in {"interrupted", "incomplete"}
                        else "terminal"
                    ),
                )
            except Exception as error:
                # Checkpoint 是加速快照，失败不能阻断已经完成的 Agent Run。
                self.record_runtime_event(
                    "checkpoint_failed",
                    {"message": f"写入 Checkpoint 失败：{error}"},
                )

        # 3. 仅投影当前 Run 的记录，并原子保存 Report 和 Session 索引。
        try:
            records = self.store.read_transcript(start_sequence)
            report = build_run_report(
                records,
                run_id=run_id,
                session_id=self.store.session_id or "未初始化会话",
            )
            return self.store.persist_run_report(report.to_dict())
        except Exception as error:
            # Report 是可重新生成的派生视图，失败不能覆盖已经形成的 Agent 终态。
            self.record_runtime_event(
                "run_report_failed",
                {"run_id": run_id, "message": f"生成运行报告失败：{error}"},
            )
            return None
        finally:
            self._active_run_id = None
            self._active_run_start_sequence = None

    @session_write
    def create_checkpoint(
        self,
        *,
        context_manager: Any,
        plan: Plan | None,
        reason: CheckpointReason,
        loop_state: Any | None,
        run_status: CheckpointRunStatus,
    ) -> Checkpoint | None:
        """在已经完成的 Turn 安全边界提交 Checkpoint。"""
        if self.store is None or self.store.session_id is None:
            return None

        # 1. 快照先覆盖已落盘的领域事实；未提交成功前不会写 committed 事件。
        checkpoint_store = CheckpointStore(self.store.session_dir)
        checkpoint = checkpoint_store.write(
            session_id=self.store.session_id,
            transcript_sequence=self.store.next_sequence - 1,
            context_manager=context_manager,
            plan=plan,
            reason=reason,
            loop_state=loop_state,
            run_status=run_status,
        )

        # 2. Transcript 记录快照提交事实，是恢复选择快照的唯一依据。
        self.record_runtime_event(
            "checkpoint_committed",
            {
                "checkpoint_id": checkpoint.checkpoint_id,
                "transcript_sequence": checkpoint.transcript_sequence,
                "checksum": checkpoint.checksum,
                "reason": checkpoint.reason,
                "run_status": checkpoint.run_status,
            },
        )

        # 3. 索引是可再生加速视图，失败不影响已经提交的恢复事实。
        try:
            checkpoint_store.update_index(checkpoint)
        except Exception as error:
            self.record_runtime_event(
                "checkpoint_index_failed",
                {"checkpoint_id": checkpoint.checkpoint_id, "message": str(error)},
            )
        return checkpoint

    def record_context_messages(
        self,
        messages: list[Any],
        *,
        completes_round: bool,
        source: str,
    ) -> None:
        """原子记录一次 Context 消息追加及其完整轮次语义。"""
        self.record_runtime_event(
            "context_messages_appended",
            {
                "messages": messages,
                "completes_round": completes_round,
                "source": source,
            },
        )

    def record_runtime_event(self, event_type: str, payload: Any) -> None:
        """记录不进入模型 Context 的完整 Session 事实。"""
        if self.store is not None:
            self.store.append_transcript(event_type, payload)

    def has_runtime_effect(self, command_id: str, effect: str) -> bool:
        """按 durable Transcript 判断 Integration effect 是否已提交。"""
        if self.store is None:
            return False
        return any(
            record.get("type") == "runtime_control_effect"
            and (record.get("payload") or {}).get("command_id") == command_id
            and (record.get("payload") or {}).get("effect") == effect
            for record in self.store.read_transcript()
        )

    def record_runtime_control_effect(
        self, command_id: str, effect: str, payload: dict[str, Any] | None = None,
    ) -> bool:
        """提交一次幂等控制 effect；返回是否由本次调用首次提交。"""
        if self.has_runtime_effect(command_id, effect):
            return False
        self.record_runtime_event("runtime_control_effect", {
            "command_id": command_id, "effect": effect, **(payload or {}),
        })
        return True

    @property
    def cancellation_committed(self) -> bool:
        """当前 Session 是否已有 durable Cancel tombstone。"""
        if self.store is None:
            return False
        return any(
            record.get("type") == "runtime_control_effect"
            and (record.get("payload") or {}).get("effect") == "cancelled"
            for record in self.store.read_transcript()
        )

    def pending_steering_intents(self) -> list[dict[str, Any]]:
        """返回尚未提交 steering_applied 的 durable steering intent。"""
        if self.store is None:
            return []
        records = self.store.read_transcript()
        applied = {
            (record.get("payload") or {}).get("command_id")
            for record in records
            if record.get("type") == "runtime_control_effect"
            and (record.get("payload") or {}).get("effect") == "steering_applied"
        }
        return [
            record.get("payload") or {}
            for record in records
            if record.get("type") == "runtime_control_effect"
            and (record.get("payload") or {}).get("effect") == "steering_intent"
            and (record.get("payload") or {}).get("command_id") not in applied
            and not self.steering_context_mutations(
                (record.get("payload") or {}).get("command_id")
            )
        ]

    def steering_intents(self) -> list[dict[str, Any]]:
        """返回所有 durable Steering intent，供恢复阶段补齐 marker。"""
        if self.store is None:
            return []
        return [
            record.get("payload") or {}
            for record in self.store.read_transcript()
            if record.get("type") == "runtime_control_effect"
            and (record.get("payload") or {}).get("effect") == "steering_intent"
        ]

    def steering_context_mutations(self, command_id: str) -> list[dict[str, Any]]:
        """按 commandId 查找已经写入 Context 的 Steering mutation。"""
        if self.store is None:
            return []
        return [
            record.get("payload") or {}
            for record in self.store.read_transcript()
            if record.get("type") == "context_user_query_appended"
            and (record.get("payload") or {}).get("source") == "steering"
            and (record.get("payload") or {}).get("command_id") == command_id
        ]

    def upsert_context_records(
        self,
        candidates: list[ContextRecordInput],
        *,
        processed_sequence: int | None = None,
    ) -> tuple[ContextRecordMutation, ...]:
        """提交核心上下文记录变更，并同步事实日志与 JSON 投影。"""
        # 1. Store 只执行可解释的新增、来源合并和显式替代。
        if self.store is not None:
            for candidate in candidates:
                if any(
                    sequence >= self.store.next_sequence
                    for sequence in candidate.source_sequences
                ):
                    raise ValueError("ContextRecord 证据序号尚未落盘")
        staged = ContextRecordStore(self.context_records.records)
        mutations = staged.upsert(candidates)
        if processed_sequence is not None:
            if (
                self.store is None
                or type(processed_sequence) is not int
                or not self.last_extracted_transcript_sequence
                < processed_sequence
                < self.store.next_sequence
            ):
                raise ValueError("ContextRecord 提取进度不在有效的已落盘范围内")
            if any(
                sequence > processed_sequence
                for candidate in candidates
                for sequence in candidate.source_sequences
            ):
                raise ValueError("ContextRecord 证据超出本次提取范围")
            # 自动提取将候选变更与进度作为一个事实提交，空结果也可恢复。
            self.record_runtime_event(
                "context_record_extraction_committed",
                {
                    "through_sequence": processed_sequence,
                    "mutations": [
                        {"action": mutation.action, "record": asdict(mutation.record)}
                        for mutation in mutations
                    ],
                },
            )
            self.context_records = staged
            self.last_extracted_transcript_sequence = processed_sequence
            try:
                self.store.persist_context_records(staged.to_dict())
            except OSError:
                self.record_runtime_event("context_record_projection_failed", {
                    "message": "记录变更已提交，JSON 投影可由 Transcript 重建",
                })
            return mutations
        if not mutations:
            return ()

        # 2. 每个变更先写入不可变 Transcript，投影文件不是恢复权威来源。
        for mutation in mutations:
            self.record_runtime_event(
                "context_record_changed",
                {
                    "action": mutation.action,
                    "record": asdict(mutation.record),
                },
            )

        self.context_records = staged
        # 3. 记录投影写入失败不能撤销已提交事实，后续恢复可重建该文件。
        if self.store is not None:
            self.store.persist_context_records(self.context_records.to_dict())
        return mutations

    def reselect_context_records(
        self,
        query: UserQuery,
        limits: ContextRetrievalLimits,
        cached: tuple[RetrievedContextRecord, ...],
    ) -> tuple[RetrievedContextRecord, ...]:
        """按当前有效记录重新选择；缓存不是权威，不加载历史文件。"""
        fallback = is_context_reference_query(query.content)
        selected = ContextRecordRetriever(limits).retrieve(
            query.content, self.context_records.active_records,
            allow_recent_fallback=fallback,
        )
        if selected == cached:
            return selected
        return self.retrieve_context_records(
            query.content, (query.sequence,), limits,
            allow_recent_fallback=fallback,
        )

    def retrieve_context_records(
        self,
        query: str,
        query_sequences: tuple[int, ...],
        limits: ContextRetrievalLimits,
        *,
        allow_recent_fallback: bool = False,
    ) -> tuple[RetrievedContextRecord, ...]:
        """为新 Query 召回 Session 历史记录并保存可观测事实。"""
        # 1. 检索器只读取 active 投影，不修改 Record 和用户原始 Query。
        records = ContextRecordRetriever(limits).retrieve(
            query,
            self.context_records.active_records,
            allow_recent_fallback=allow_recent_fallback,
        )

        # 2. Transcript 只记录引用、评分与原因，不重复复制 Record 正文。
        self.record_runtime_event(
            "context_records_retrieved",
            {
                "context_query_sequences": list(query_sequences),
                "records": [
                    {
                        "record_id": record.record_id,
                        "score": record.score,
                        "match_reasons": list(record.match_reasons),
                    }
                    for record in records
                ],
                "total_characters": sum(len(record.content) for record in records),
                "recent_fallback": allow_recent_fallback,
            },
        )
        return records

    def record_compaction(self, boundary: dict[str, Any]) -> dict[str, Any]:
        """权威日志先提交；调用方随后发布内存投影。"""
        if self.store is None:
            raise RuntimeError("COMPACTION_DURABLE_STORE_REQUIRED")
        metrics = self.store.commit_compaction(boundary)
        from ..observation import current_observation
        observer = current_observation.get()
        if observer:
            observer.emit("compaction_transcript_published", **metrics)
        return metrics

    def record_plan_state(
        self,
        plan: Plan | None,
        *,
        action: str,
        reason: str | None = None,
    ) -> None:
        """记录 PlanStore 已经提交的完整当前状态。"""
        self.record_runtime_event(
            "plan_state_changed",
            {
                "action": action,
                "reason": reason,
                "plan": asdict(plan) if plan is not None else None,
            },
        )
