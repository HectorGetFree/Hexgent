
"""入口无关的 AgentEngine 与 Hexgent 命令行适配。"""

from collections.abc import Generator
from dataclasses import replace
from pathlib import Path
from threading import Lock
from typing import Any

from hexgent.events import (
    AgentEvent,
    ContextCompactionFailedEvent,
    ContextCompactionFinishedEvent,
    ContextCompactionStartedEvent,
    LoopLimitReachedEvent,
    RunFinishedEvent,
    PlanCreatedEvent,
    PlanUpdatedEvent,
    TextDeltaEvent,
    ToolFinishedEvent,
    ToolStartedEvent,
    ToolExecutionCompletedEvent,
    ToolBatchFinishedEvent,
    ToolBatchStartedEvent,
)
from .config import HexgentConfig, RunOptions, load_config, validate_config
from .context import ContextManager
from .loop.agent_loop import agent_loop
from .loop.loop_state import LoopState
from .run_control import RunControl
from .loop.limits import stop_reason_message
from .plan import InMemoryPlanStore, Plan
from .session import Session, SessionSettings
from .terminal_io import TerminalIO
from .tool.tool_context import ToolExecutionContext
from .tool.tool_execution import UserConfirmation, unavailable_user_confirmation
from .workspace import find_workspace_root


TOOL_STATUS_LABELS = {
    "success": "成功",
    "invalid": "参数无效",
    "denied": "已拒绝",
    "error": "执行错误",
    "deduplicated": "已去重",
    "skipped": "未执行",
}

PLAN_STATUS_LABELS = {
    "pending": "待处理",
    "in_progress": "进行中",
    "completed": "已完成",
    "blocked": "已阻塞",
}


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


def _print_plan(plan: Plan, title: str) -> None:
    """展示 Plan 的整体状态和所有步骤状态。"""
    plan_status = PLAN_STATUS_LABELS[plan.status]
    print(f"\033[90m{title}：{plan.objective}\033[0m")
    print(
        f"\033[90m  整体状态：{plan_status}，计划版本：{plan.revision}\033[0m"
    )
    for index, step in enumerate(plan.steps, start=1):
        step_status = PLAN_STATUS_LABELS[step.status]
        print(
            f"\033[90m  {index}. [{step_status}] {step.content} "
            f"({step.step_id})\033[0m"
        )
    if plan.last_replan_reason:
        print(f"\033[90m  调整原因：{plan.last_replan_reason}\033[0m")
    if plan.blocked_reason:
        print(f"\033[90m  阻塞原因：{plan.blocked_reason}\033[0m")


def main(config_path: Path | None = None) -> None:
    """启动 Hexgent 交互会话。"""
    # 1. 初始化终端输入、对话历史和会话级工具上下文。
    terminal_io = TerminalIO()
    workdir = find_workspace_root()
    config = load_config(workdir, config_path)
    engine = AgentEngine(config, workdir)

    print("\033[36mHexgent：\033[0m你好！有什么可以帮你的？")
    print("输入内容后按两次回车或输入 /send 发送消息；exit 或 q 可以结束对话。\n")

    # 2. 持续接收用户输入，并复用同一个 PlanStore 处理多轮对话。
    while True:
        try:
            query = terminal_io.read_message()
        except EOFError:
            print("\n输入流已关闭，会话结束。")
            break
        except KeyboardInterrupt:
            print("\n会话已结束。")
            break

        normalized_query = query.strip().lower()
        if normalized_query in ("q", "exit"):
            break
        if not normalized_query:
            continue

        print("\033[90m正在处理，请稍候……\033[0m")

        # 3. 为本轮运行创建独立中断控制器，并在消费事件流期间接收 Ctrl+C。
        text_is_open = False
        execution_completed_calls: dict[str, str] = {}
        run_control = RunControl()
        event_stream = engine.run(
            query,
            user_confirmation=terminal_io.request_confirmation,
            run_control=run_control,
        )
        with terminal_io.capture_run_interrupt(run_control):
            for event in event_stream:
                if isinstance(event, TextDeltaEvent):
                    if not text_is_open:
                        print("\033[36mHexgent：\033[0m", end="", flush=True)
                        text_is_open = True
                    print(event.delta, end="", flush=True)
                elif isinstance(event, ToolStartedEvent):
                    if text_is_open:
                        print()
                    print(f"\033[90m开始处理工具：{event.description}\033[0m")
                    text_is_open = False
                elif isinstance(event, ToolExecutionCompletedEvent):
                    execution_completed_calls[event.call_id] = event.status
                    status_label = TOOL_STATUS_LABELS[event.status]
                    print(
                        f"\033[90m工具结束：{event.tool_name}，状态：{status_label}\033[0m"
                    )
                elif isinstance(event, ToolFinishedEvent):
                    execution_status = execution_completed_calls.get(event.call_id)
                    if execution_status == event.status:
                        continue
                    status_label = TOOL_STATUS_LABELS[event.status]
                    prefix = "工具结果处理结束" if execution_status is not None else "工具结束"
                    print(
                        f"\033[90m{prefix}：{event.tool_name}，状态：{status_label}\033[0m"
                    )
                elif isinstance(event, ToolBatchStartedEvent):
                    print(
                        f"\033[90m开始并行处理 {event.tool_count} 个工具"
                        f"（最大并发：{event.max_workers}）\033[0m"
                    )
                elif isinstance(event, ToolBatchFinishedEvent):
                    print(
                        f"\033[90m并行工具批次完成：成功 {event.succeeded_count}，"
                        f"失败 {event.failed_count}，未执行 {event.skipped_count}，"
                        f"用时 {event.elapsed_seconds:.1f} 秒\033[0m"
                    )
                elif isinstance(event, PlanCreatedEvent):
                    _print_plan(event.plan, "执行计划")
                elif isinstance(event, PlanUpdatedEvent):
                    _print_plan(event.plan, "计划进度")
                elif isinstance(event, LoopLimitReachedEvent):
                    print(f"\033[90m{stop_reason_message(event.reason)}\033[0m")
                elif isinstance(event, ContextCompactionStartedEvent):
                    print("\033[90m正在整理较早的对话上下文……\033[0m")
                elif isinstance(event, ContextCompactionFinishedEvent):
                    label = "上下文部分整理完成" if event.status == "partial" else "上下文整理完成"
                    print(
                        f"\033[90m{label}："
                        f"约 {event.before_tokens:,} → {event.after_tokens:,} Token\033[0m"
                    )
                elif isinstance(event, ContextCompactionFailedEvent):
                    print(f"\033[90m{event.message}\033[0m")
                elif isinstance(event, RunFinishedEvent):
                    if text_is_open:
                        print()
                    if event.result.status == "blocked":
                        plan = engine.tool_context.plan_store.get_plan()
                        reason = plan.blocked_reason if plan is not None else None
                        detail = f"：{reason}" if reason else ""
                        print(f"\033[90mPlan 已阻塞{detail}。可以补充信息后继续。\033[0m")
                    elif event.result.status == "incomplete":
                        print(
                            "\033[90mPlan 尚未完成，本次运行已停止；"
                            "可以输入“继续”恢复执行。\033[0m"
                        )
                    elif event.result.status == "interrupted":
                        print(
                            "\033[90m当前运行已中断；Session、Context 和 Plan 已保留，"
                            "可以输入“继续”恢复执行。\033[0m"
                        )
                    elif event.result.status == "cancelled":
                        print(
                            "\033[90m当前任务已取消；后续操作已停止，"
                            "取消前已经完成的文件修改和其他副作用仍然保留。\033[0m"
                        )
                    print(
                        f"\033[90m本次用时：{event.result.elapsed_seconds:.1f} 秒\033[0m\n"
                    )
                    text_is_open = False
