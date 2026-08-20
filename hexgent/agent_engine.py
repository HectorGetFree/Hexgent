
"""Hexgent 命令行入口。"""

from .stream_events import (
    LoopLimitReachedEvent,
    RunFinishedEvent,
    TaskCreatedEvent,
    TextDeltaEvent,
    ToolFinishedEvent,
    ToolStartedEvent,
)
from .agent_loop import agent_loop_stream
from .loop_control import stop_reason_message
from .task import InMemoryTaskStore
from .tool.tool_context import ToolExecutionContext


TOOL_STATUS_LABELS = {
    "success": "成功",
    "invalid": "参数无效",
    "denied": "已拒绝",
    "error": "执行错误",
    "skipped": "未执行",
}

TASK_STATUS_LABELS = {
    "pending": "待处理",
    "in_progress": "进行中",
    "completed": "已完成",
    "blocked": "已阻塞",
}


def configure_readline() -> None:
    """readline 可用时，为 macOS libedit 配置 UTF-8 输入。"""
    try:
        import readline
    except ImportError:
        return

    readline.parse_and_bind("set bind-tty-special-chars off")
    readline.parse_and_bind("set input-meta on")
    readline.parse_and_bind("set output-meta on")
    readline.parse_and_bind("set convert-meta off")


def main() -> None:
    """启动 Hexgent 交互会话。"""
    # 1. 初始化终端输入、对话历史和会话级工具上下文。
    configure_readline()
    history = []
    tool_context = ToolExecutionContext(InMemoryTaskStore())

    print("\033[36mHexgent：\033[0m你好！有什么可以帮你的？")
    print("输入 exit 或 q 可以结束对话。\n")

    # 2. 持续接收用户输入，并复用同一个 TaskStore 处理多轮对话。
    while True:
        try:
            query = input("\033[32m你：\033[0m")
        except (EOFError, KeyboardInterrupt):
            break

        if query.strip().lower() in ("q", "exit", ""):
            break

        history.append({"role": "user", "content": query})

        # 3. 消费 Agent 事件流，实时展示模型文字和弱化的工具运行状态。
        text_is_open = False
        for event in agent_loop_stream(history, context=tool_context):
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
            elif isinstance(event, ToolFinishedEvent):
                status_label = TOOL_STATUS_LABELS[event.status]
                print(
                    f"\033[90m工具结束：{event.tool_name}，状态：{status_label}\033[0m"
                )
            elif isinstance(event, TaskCreatedEvent):
                print(f"\033[90m任务计划：{event.task.objective}\033[0m")
                for index, step in enumerate(event.task.steps, start=1):
                    status_label = TASK_STATUS_LABELS[step.status]
                    print(f"\033[90m  {index}. [{status_label}] {step.content}\033[0m")
            elif isinstance(event, LoopLimitReachedEvent):
                print(f"\033[90m{stop_reason_message(event.reason)}\033[0m")
            elif isinstance(event, RunFinishedEvent):
                if text_is_open:
                    print()
                print(
                    f"\033[90m本次用时：{event.result.elapsed_seconds:.1f} 秒\033[0m\n"
                )
                text_is_open = False
