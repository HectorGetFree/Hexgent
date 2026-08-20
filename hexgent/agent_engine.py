
"""Hexgent 命令行入口。"""

from .agent_loop import agent_loop
from .task import InMemoryTaskStore
from .tool.tool_context import ToolExecutionContext


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

        result = agent_loop(history, context=tool_context)
        print(f"\033[36mHexgent：\033[0m{result.output_text}")
        print(f"\033[90m本次用时：{result.elapsed_seconds:.1f} 秒\033[0m\n")
