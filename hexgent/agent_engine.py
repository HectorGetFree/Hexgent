
"""Hexgent 命令行入口。"""

from pathlib import Path

from hexgent.events import (
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
from .config import load_config
from .context import ContextManager
from .loop.agent_loop import agent_loop
from .loop.interrupt import InterruptController
from .loop.limits import stop_reason_message
from .plan import InMemoryPlanStore, Plan
from .session import Session, SessionSettings
from .terminal_io import TerminalIO
from .tool.tool_context import ToolExecutionContext
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
    context_manager = ContextManager()
    session = Session.persistent(
        workdir,
        config.tool_result,
        SessionSettings(config.permission.default_mode),
    )
    tool_context = ToolExecutionContext(InMemoryPlanStore(), workdir)

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

        user_query = context_manager.append_user_query(query)
        session.record_user_query(user_query)
        print("\033[90m正在处理，请稍候……\033[0m")

        # 3. 为本轮运行创建独立中断控制器，并在消费事件流期间接收 Ctrl+C。
        text_is_open = False
        execution_completed_calls: dict[str, str] = {}
        interrupt_controller = InterruptController()
        event_stream = agent_loop(
            context_manager,
            context=tool_context,
            limits=config.loop,
            context_limits=config.context,
            user_confirmation=terminal_io.request_confirmation,
            session=session,
            tool_parallel_limits=config.tool_parallel,
            tool_retry_limits=config.tool_retry,
            permission_context=session.permission_context,
            interrupt_controller=interrupt_controller,
        )
        with terminal_io.capture_run_interrupt(interrupt_controller):
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
                        plan = tool_context.plan_store.get_plan()
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
                    print(
                        f"\033[90m本次用时：{event.result.elapsed_seconds:.1f} 秒\033[0m\n"
                    )
                    text_is_open = False
