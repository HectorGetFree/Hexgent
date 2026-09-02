"""把结构化 AgentEvent 渲染为终端文本。"""

from collections.abc import Callable

from ...events import (
    AgentEvent,
    ContextCompactionFailedEvent,
    ContextCompactionFinishedEvent,
    ContextCompactionStartedEvent,
    CheckpointCommittedEvent,
    LoopLimitReachedEvent,
    PlanCreatedEvent,
    PlanUpdatedEvent,
    RunFinishedEvent,
    TextDeltaEvent,
    ToolBatchFinishedEvent,
    ToolBatchStartedEvent,
    ToolExecutionCompletedEvent,
    ToolFinishedEvent,
    ToolStartedEvent,
)
from ...loop.limits import stop_reason_message
from ...plan import Plan


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

CHECKPOINT_REASON_LABELS = {
    "tool_batch_completed": "工具批次结果已回填",
    "plan_continuation_ready": "Plan 继续状态已提交",
    "context_compaction_completed": "上下文压缩已提交",
    "reactive_compaction_completed": "应急上下文压缩已提交",
    "finalization_ready": "收尾状态已提交",
    "steering_applied": "新要求已在边界生效",
    "run_finished": "运行已结束",
}


class TerminalEventRenderer:
    """维护一次 Run 的终端展示状态并消费结构化事件。"""

    def __init__(self, get_plan: Callable[[], Plan | None]) -> None:
        # 当前是否正在同一行流式展示模型文字。
        self._text_is_open = False

        # 已即时展示执行完成状态的调用，用于避免顺序提交时重复输出。
        self._execution_completed_calls: dict[str, str] = {}

        # 仅在展示终态时读取 Plan，不把 UI 查询能力放入 Core。
        self._get_plan = get_plan

    def render(self, event: AgentEvent) -> None:
        """根据事件类型更新当前终端展示。"""
        # 1. 模型文字以正常样式实时输出。
        if isinstance(event, TextDeltaEvent):
            if not self._text_is_open:
                print("\033[36mHexgent：\033[0m", end="", flush=True)
                self._text_is_open = True
            print(event.delta, end="", flush=True)
            return

        # 2. 工具、Plan 与压缩过程使用弱化的独立状态行。
        if isinstance(event, ToolStartedEvent):
            self._close_text_line()
            print(f"\033[90m开始处理工具：{event.description}\033[0m")
        elif isinstance(event, ToolExecutionCompletedEvent):
            self._execution_completed_calls[event.call_id] = event.status
            status = TOOL_STATUS_LABELS[event.status]
            print(f"\033[90m工具结束：{event.tool_name}，状态：{status}\033[0m")
        elif isinstance(event, ToolFinishedEvent):
            execution_status = self._execution_completed_calls.get(event.call_id)
            if execution_status == event.status:
                return
            status = TOOL_STATUS_LABELS[event.status]
            prefix = "工具结果处理结束" if execution_status is not None else "工具结束"
            print(f"\033[90m{prefix}：{event.tool_name}，状态：{status}\033[0m")
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
            self._print_plan(event.plan, "执行计划")
        elif isinstance(event, PlanUpdatedEvent):
            self._print_plan(event.plan, "计划进度")
        elif isinstance(event, LoopLimitReachedEvent):
            print(f"\033[90m{stop_reason_message(event.reason)}\033[0m")
        elif isinstance(event, ContextCompactionStartedEvent):
            print("\033[90m正在整理较早的对话上下文……\033[0m")
        elif isinstance(event, ContextCompactionFinishedEvent):
            label = (
                "上下文部分整理完成"
                if event.status == "partial"
                else "上下文整理完成"
            )
            print(
                f"\033[90m{label}：约 {event.before_tokens:,} → "
                f"{event.after_tokens:,} Token\033[0m"
            )
        elif isinstance(event, ContextCompactionFailedEvent):
            print(f"\033[90m{event.message}\033[0m")
        elif isinstance(event, CheckpointCommittedEvent):
            label = CHECKPOINT_REASON_LABELS[event.reason]
            print(
                f"\033[90mCheckpoint 已提交：{event.checkpoint_id}，"
                f"原因：{label}\033[0m"
            )
        elif isinstance(event, RunFinishedEvent):
            self._render_finished(event)

    def _render_finished(self, event: RunFinishedEvent) -> None:
        """展示一次 Run 的终态，并关闭可能仍打开的文字行。"""
        # 1. 流式文本已经展示过，终态不能重复输出完整回答。
        self._close_text_line()

        # 2. 根据结构化终态展示后续可采取的操作。
        if event.result.status == "blocked":
            plan = self._get_plan()
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

    def _close_text_line(self) -> None:
        """在状态行开始前结束正在流式输出的模型文字。"""
        if self._text_is_open:
            print()
            self._text_is_open = False

    @staticmethod
    def _print_plan(plan: Plan, title: str) -> None:
        """展示 Plan 的整体状态和所有步骤状态。"""
        plan_status = PLAN_STATUS_LABELS[plan.status]
        print(f"\033[90m{title}：{plan.objective}\033[0m")
        print(
            f"\033[90m  整体状态：{plan_status}，"
            f"计划版本：{plan.revision}\033[0m"
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
