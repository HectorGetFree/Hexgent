"""管理 Session 生命周期、Transcript 和会话工件。"""

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..context.context_manager import UserQuery
from ..plan import Plan
from ..tool.tool_permission_pipeline import PermissionContext, PermissionMode
from .run_report import build_run_report
from .session_store import SessionStore, ToolResultLimits
from .checkpoint import CheckpointStore


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
    ) -> "Session":
        """使用已经打开的 Store 构造可继续追加事实的 Session。"""
        session = cls(store, settings)
        session._permission_mode_recorded = True
        return session

    @property
    def permission_context(self) -> PermissionContext:
        """取得当前 Session 权限模式的不可变执行快照。"""
        return PermissionContext(self.settings.permission_mode)

    def record_user_query(self, query: UserQuery, *, source: str = "user") -> None:
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
                },
            )

    def start_run(self, *, limits_enabled: bool, plan: Plan | None) -> None:
        """开始跟踪一次由最新用户 Query 驱动的 Agent Run。"""
        if self.store is None:
            return
        if self._latest_user_query is None:
            raise RuntimeError("Session 尚未记录用户 Query，不能开始 Agent Run")
        if self._active_run_id is not None:
            raise RuntimeError("当前 Session 已有尚未结束的 Agent Run")

        self._active_run_id = self.store.next_run_id()
        self._active_run_start_sequence = self.store.next_sequence
        self.record_runtime_event(
            "agent_run_started",
            {
                "run_id": self._active_run_id,
                "limits_enabled": limits_enabled,
                "user_query": self._latest_user_query,
                "plan": asdict(plan) if plan is not None else None,
            },
        )

    def finish_run(
        self,
        *,
        status: str,
        stop_reason: str | None,
        elapsed_seconds: float,
        output_text: str,
        plan: Plan | None,
        context_manager: Any | None = None,
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
                CheckpointStore(self.store.session_dir).write(
                    session_id=self.store.session_id,
                    transcript_sequence=self.store.next_sequence - 1,
                    context_manager=context_manager,
                    plan=plan,
                    reason="run_finished",
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

    def record_compaction(self, boundary: dict[str, Any]) -> None:
        """记录一次已经提交、可确定性重放的 Context 压缩。"""
        self.record_runtime_event("context_compaction_applied", boundary)

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
