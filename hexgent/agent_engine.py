
"""与外部入口无关的 Hexgent Agent Core 门面。"""

from collections.abc import Generator
from dataclasses import asdict
from dataclasses import replace
from pathlib import Path
from threading import Lock
from typing import Any

from hexgent.events import AgentEvent
from .config import HexgentConfig, RunOptions, validate_config
from .context import ContextManager
from .loop.agent_loop import agent_loop
from .loop.loop_state import LoopState
from .run_control import RunControl
from .plan import InMemoryPlanStore
from .session import Session, SessionRestorer, SessionSettings
from .tool.tool_context import ToolExecutionContext
from .tool.tool_execution import UserConfirmation, unavailable_user_confirmation


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
        self.tool_context = tool_context or ToolExecutionContext(
            InMemoryPlanStore(),
            self.workdir,
        )

        # 同一个 Engine 的 Context、Session 和工具状态只能由一个 Run 驱动。
        self._run_lock = Lock()
        self._run_active = False

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
        )

    def resolve_config(self, options: RunOptions | None = None) -> HexgentConfig:
        """合并用户覆盖项，并返回本次 Run 最终使用的 HexgentConfig。"""
        overrides = options or RunOptions()
        effective_config = replace(
            self.config,
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
        )
        return validate_config(effective_config)

    def run(
        self,
        query: str,
        *,
        options: RunOptions | None = None,
        user_confirmation: UserConfirmation = unavailable_user_confirmation,
        run_control: RunControl | None = None,
        client: Any | None = None,
    ) -> Generator[AgentEvent, None, None]:
        """接受原始 Query，构造活动 State，并启动事件流。"""
        # 1. 事件流首次消费时原子占用 Engine，拒绝并发 Run 污染共享状态。
        with self._run_lock:
            if self._run_active:
                raise RuntimeError("当前 AgentEngine 已有正在执行的 Agent Run")
            self._run_active = True

        try:
            # 2. 在修改 Context 前生成并校验本次 Run 的完整配置快照。
            effective_config = self.resolve_config(options)

            # 3. 用户 Query 同时进入活动 Context 和 Session 事实日志。
            user_query = self.context_manager.append_user_query(query)
            self.session.record_user_query(user_query)

            # 4. LoopState 只保存会演进的活动状态，不包含配置和外部依赖。
            state = LoopState(
                context_manager=self.context_manager,
                tool_context=self.tool_context,
            )

            # 5. 稳定配置和依赖作为显式参数传入 Loop，并转发完整事件流。
            yield from agent_loop(
                state,
                config=effective_config,
                client=client,
                session=self.session,
                user_confirmation=user_confirmation,
                run_control=run_control or RunControl(),
            )
        finally:
            # 6. 正常结束、异常或调用方关闭事件流时都释放 Engine 占用标记。
            with self._run_lock:
                self._run_active = False

    def cancel_pending_task(self, reason: str = "用户取消暂停任务") -> None:
        """在没有活跃 Run 时终结保留的 Plan，不回滚已经发生的副作用。"""
        # 1. 暂停 Run 完全收束前不允许并发修改共享 PlanStore。
        with self._run_lock:
            if self._run_active:
                raise RuntimeError("当前 AgentEngine 仍有正在执行的 Agent Run")

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
