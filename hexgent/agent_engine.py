
"""Hexgent 命令行入口。"""

from .agent_loop import agent_loop


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
    configure_readline()
    history = []

    print("\033[36mHexgent：\033[0m你好！有什么可以帮你的？")
    print("输入 exit 或 q 可以结束对话。\n")

    while True:
        try:
            query = input("\033[32m你：\033[0m")
        except (EOFError, KeyboardInterrupt):
            break

        if query.strip().lower() in ("q", "exit", ""):
            break

        history.append({"role": "user", "content": query})
        answer = agent_loop(history)
        print(f"\033[36mHexgent：\033[0m{answer}\n")
