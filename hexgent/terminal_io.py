"""Hexgent 命令行的统一输入输出边界。"""

from .tool.tool_permission import UserConfirmationResult


class TerminalIO:
    """统一读取用户消息和工具权限确认。"""

    def read_message(self) -> str:
        """持续读取多行内容，直到输入空行或 /send 后提交。"""
        # 1. 只在第一行展示用户提示，避免粘贴多行时重复绘制续行符号。
        lines: list[str] = []
        while True:
            prompt = "\033[32m你：\033[0m" if not lines else ""
            line = input(prompt)

            # 2. /send 或空行结束消息，实现输入内容后按两次回车提交。
            if not lines and line.strip().lower() in {"q", "exit"}:
                return line
            if line.strip().lower() == "/send":
                return "\n".join(lines)
            if not line:
                return "\n".join(lines)
            lines.append(line)

    def request_confirmation(
        self,
        tool_name: str,
        message: str,
    ) -> UserConfirmationResult:
        """等待用户确认是否允许本次工具调用。"""
        prompt = (
            f"\033[35m权限确认：{message}（工具：{tool_name}），"
            "是否允许？[y/N] \033[0m"
        )
        try:
            answer = input(prompt)
        except EOFError:
            print("\n当前输入环境无法读取权限确认。")
            return "unavailable"
        except KeyboardInterrupt:
            print()
            return "deny"
        if answer.strip().lower() in {"y", "yes", "是", "允许"}:
            return "allow"
        return "deny"
