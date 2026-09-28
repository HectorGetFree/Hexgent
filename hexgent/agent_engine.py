
"""与外部入口无关的 Hexgent Agent Core 门面。"""

from collections.abc import Generator
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from threading import Lock
from typing import Any, Literal

from hexgent.events import AgentEvent
from .config import HexgentConfig, RunOptions, validate_config
from .context import ContextManager
from .loop.agent_loop import agent_loop
from .loop.loop_state import LoopState, RunStateSnapshot
from .loop.transitions import Stop
from .run_control import RunControl
from .plan import InMemoryPlanStore
from .memory import MemoryRetriever, MemoryStore
from .utils.retrieval_text import is_context_reference_query
from .session import (
    ContextRecordInput,
    ContextRecordMutation,
    Session,
    SessionRestorer,
    SessionSettings,
)
from .tool.tool_context import ToolExecutionContext
from .tool.tool_execution import UserConfirmation, unavailable_user_confirmation


RunIntent = Literal["submit", "continue"]


@dataclass(frozen=True)
class RunRequest:
    """一次 Session 内 Run 的明确意图，不承担 Session 选择职责。"""

    # submit 写入新的用户 Query；continue 从可续跑 Checkpoint 继续原状态。
    intent: RunIntent

    # submit 必填，continue 必须为空，防止“续跑”悄然变成新的对话。
    query: str | None = None

    # 仅本次 Run 可覆盖的配置，不改变 Engine 创建时的基础配置。
    options: RunOptions | None = None

    @classmethod
    def submit(
        cls,
        query: str,
        *,
        options: RunOptions | None = None,
    ) -> "RunRequest":
        """构造一条新的用户请求。"""
        return cls("submit", query, options)

    @classmethod
    def continue_run(
        cls,
        *,
        options: RunOptions | None = None,
    ) -> "RunRequest":
        """构造一次不追加 Query 的恢复执行请求。"""
        return cls("continue", None, options)


class AgentEngine:
    """持有会话能力，并把 HexgentConfig 收敛为一次次 Agent Run。"""

    def __init__(
        self,
        config: HexgentConfig,
        workdir: Path,
        *,
        context_manager: ContextManager | None = None,
        session: Session | None = None,
        tool_context: ToolExecutionContext | None = None,
        memory_store: MemoryStore | None = None,
        pending_run_state: RunStateSnapshot | None = None,
    ) -> None:
        # 进程级配置只在 Engine 创建时注入，后续 Run 从这里生成有效快照。
        self.config = validate_config(config)

        # 工作区在 Engine 生命周期内保持稳定，避免权限与工具路径边界漂移。
        self.workdir = workdir.resolve()

        # Context、Session 和工具上下文跨多个用户 Query 复用。
        self.context_manager = context_manager or ContextManager()
        self.session = session or Session.persistent(
            self.workdir,
            config.tool_result,
            SessionSettings(config.permission.default_mode),
        )
        if tool_context is not None:
            self.tool_context = tool_context
            self.memory_store = memory_store or tool_context.memory_store
        else:
            self.memory_store = memory_store or MemoryStore(self.workdir)
            self.tool_context = ToolExecutionContext(
                InMemoryPlanStore(), self.workdir, self.memory_store,
            )

        # 恢复后尚未执行的 Checkpoint 状态；新的 submit 会显式取代它。
        self._pending_run_state = pending_run_state

        # 同一个 Engine 的 Context、Session 和工具状态只能由一个 Run 驱动。
        self._run_lock = Lock()
        self._run_active = False

    @classmethod
    def create(cls, config: HexgentConfig, workdir: Path) -> "AgentEngine":
        """创建一个绑定新 Session 的 AgentEngine。"""
        return cls(config, workdir)

    @classmethod
    def restore(
        cls,
        config: HexgentConfig,
        workdir: Path,
        session_id: str,
    ) -> "AgentEngine":
        """从持久化 Transcript 恢复一个可继续运行的 Agent Core。"""
        # 1. 恢复器只负责把会话事实重建为三个独立核心对象。
        restored = SessionRestorer(
            workdir,
            config.tool_result,
        ).restore(session_id)

        # 2. 新 Engine 使用当前进程配置，但继承原 Session 的权限和活动状态。
        return cls(
            config,
            workdir,
            context_manager=restored.context_manager,
            session=restored.session,
            tool_context=restored.tool_context,
            pending_run_state=restored.pending_run_state,
        )

    def resolve_config(self, options: RunOptions | None = None) -> HexgentConfig:
        """合并用户覆盖项，并返回本次 Run 最终使用的 HexgentConfig。"""
        overrides = options or RunOptions()
        effective_config = replace(
            self.config,
            context_extraction=(
                overrides.context_extraction
                if overrides.context_extraction is not None
                else self.config.context_extraction
            ),
            loop=overrides.loop if overrides.loop is not None else self.config.loop,
            context=(
                overrides.context
                if overrides.context is not None
                else self.config.context
            ),
            tool_parallel=(
                overrides.tool_parallel
                if overrides.tool_parallel is not None
                else self.config.tool_parallel
            ),
            tool_retry=(
                overrides.tool_retry
                if overrides.tool_retry is not None
                else self.config.tool_retry
            ),
            context_retrieval=(
                overrides.context_retrieval
                if overrides.context_retrieval is not None
                else self.config.context_retrieval
            ),
            memory_retrieval=(
                overrides.memory_retrieval
                if overrides.memory_retrieval is not None
                else self.config.memory_retrieval
            ),
        )
        return validate_config(effective_config)

    def run(
        self,
        request: RunRequest | str,
        *,
        options: RunOptions | None = None,
        user_confirmation: UserConfirmation = unavailable_user_confirmation,
        run_control: RunControl | None = None,
        client: Any | None = None,
        workspace_run_id: str | None = None,
        conversation_id: str | None = None,
    ) -> Generator[AgentEvent, None, None]:
        """执行当前 Engine 固定 Session 内的一次提交或续跑。"""
        # 1. 事件流首次消费时原子占用 Engine，拒绝并发 Run 污染共享状态。
        with self._run_lock:
            if self._run_active:
                raise RuntimeError("当前 AgentEngine 已有正在执行的 Agent Run")
            self._run_active = True

        try:
            # 2. 兼容旧的字符串入口，但统一转换为带明确意图的 RunRequest。
            normalized_request = self._normalize_request(request, options)
            effective_config = self.resolve_config(normalized_request.options)

            # 3. submit 产生新事实；continue 只重建 Checkpoint 的活动 State。
            state = self._build_run_state(normalized_request, effective_config)
            if workspace_run_id is not None and conversation_id is not None:
                self.session.bind_runtime_run(workspace_run_id, conversation_id)

            # 4. 稳定配置和依赖作为显式参数传入 Loop，并转发完整事件流。
            outcome = yield from agent_loop(
                state,
                config=effective_config,
                client=client,
                session=self.session,
                user_confirmation=user_confirmation,
                run_control=run_control or RunControl(),
            )

            # 5. 终态为暂停或未完成时，当前进程也保留同一份可继续状态。
            if isinstance(outcome, Stop) and outcome.terminal.status in {
                "interrupted",
                "incomplete",
            }:
                self._pending_run_state = outcome.state.to_snapshot()
            elif isinstance(outcome, Stop):
                self._pending_run_state = None
        finally:
            # 6. 正常结束、异常或调用方关闭事件流时都释放 Engine 占用标记。
            with self._run_lock:
                self._run_active = False

    def _normalize_request(
        self,
        request: RunRequest | str,
        options: RunOptions | None,
    ) -> RunRequest:
        """将兼容入口收束为唯一的 RunRequest 协议。"""
        if isinstance(request, str):
            return RunRequest.submit(request, options=options)
        if not isinstance(request, RunRequest):
            raise TypeError("run() 只接受 RunRequest 或字符串 Query")
        if options is not None:
            raise ValueError("RunRequest 已包含 options，不能重复传入 options")
        if request.intent == "submit":
            if not isinstance(request.query, str) or not request.query.strip():
                raise ValueError("submit RunRequest 必须包含非空 query")
        elif request.intent == "continue":
            if request.query is not None:
                raise ValueError("continue RunRequest 不能携带 query")
        else:
            raise ValueError(f"不支持的 RunRequest intent：{request.intent}")
        return request

    def _build_run_state(
        self,
        request: RunRequest,
        config: HexgentConfig,
    ) -> LoopState:
        """根据 Run 意图构造新 State 或恢复既有 State。"""
        if request.intent == "submit":
            if self._pending_run_state is not None:
                self.session.record_runtime_event(
                    "agent_run_superseded",
                    {"reason": "新的用户 Query 取代了可继续的 Agent Run"},
                )
                self._pending_run_state = None
            user_query = self.context_manager.append_user_query(request.query or "")
            self.session.record_user_query(user_query)
            relevant_context = self.session.retrieve_context_records(
                request.query or "",
                (user_query.sequence,),
                config.context_retrieval,
                allow_recent_fallback=is_context_reference_query(request.query or ""),
            )
            relevant_memory = MemoryRetriever(config.memory_retrieval).retrieve(
                request.query or "", self.memory_store.items,
            )
            return LoopState(
                context_manager=self.context_manager,
                tool_context=self.tool_context,
                relevant_context=relevant_context,
                relevant_memory=relevant_memory,
            )

        if self._pending_run_state is None:
            raise RuntimeError("当前 Session 没有可继续的 Agent Run")
        snapshot = self._pending_run_state
        return LoopState.from_snapshot(
            snapshot,
            context_manager=self.context_manager,
            tool_context=self.tool_context,
        )

    @property
    def has_continuable_run(self) -> bool:
        """当前 Engine 是否持有可由 continue 请求恢复的 Checkpoint 状态。"""
        return self._pending_run_state is not None

    def cancel_pending_task(self, reason: str = "用户取消暂停任务") -> None:
        """在没有活跃 Run 时终结保留的 Plan，不回滚已经发生的副作用。"""
        # 1. 暂停 Run 完全收束前不允许并发修改共享 PlanStore。
        with self._run_lock:
            if self._run_active:
                raise RuntimeError("当前 AgentEngine 仍有正在执行的 Agent Run")

            self._pending_run_state = None

            # 2. 取消前先把 Plan 快照写入 Session 事实，再清除继续执行义务。
            plan = self.tool_context.plan_store.get_plan()
            if plan is None:
                return
            self.session.record_runtime_event(
                "plan_cancelled",
                {"reason": reason, "plan": asdict(plan)},
            )
            self.tool_context.plan_store.clear_plan()
            self.session.record_plan_state(
                None,
                action="cleared",
                reason="cancelled",
            )

    def upsert_context_records(
        self,
        candidates: list[ContextRecordInput],
    ) -> tuple[ContextRecordMutation, ...]:
        """在安全 Run 边界外提交 Session 核心上下文记录。"""
        # 1. 条目来源必须已经形成 Transcript 事实，活跃 Run 中不允许并发插入。
        with self._run_lock:
            if self._run_active:
                raise RuntimeError("当前 Agent Run 未结束，不能更新 ContextRecord")

            # 2. Session 负责追加事实、更新 JSON 投影并返回精确变更结果。
            return self.session.upsert_context_records(candidates)
