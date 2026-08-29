"""Hexgent 命令行的统一输入输出边界。"""

from collections.abc import Iterator
from contextlib import contextmanager
import signal
import threading

from .loop.interrupt import InterruptController
from .tool.tool_permission_pipeline import UserConfirmationResult


class TerminalIO:
    """统一读取用户消息和工具权限确认。"""

    @contextmanager
    def capture_run_interrupt(
        self,
        interrupt_controller: InterruptController,
    ) -> Iterator[None]:
        """在当前 Agent 运行期间将 Ctrl+C 转换为本轮中断信号。"""
        # 1. 非主线程不能注册进程信号处理器，此时仅保留控制器的程序化中断能力。
        if threading.current_thread() is not threading.main_thread():
            yield
            return

        # 2. 保存原处理器，并为当前运行临时接管 Ctrl+C。
        previous_handler = signal.getsignal(signal.SIGINT)

        def handle_interrupt(_signum: int, _frame: object) -> None:
            """立即标记本轮中断，并唤醒已经注册的阻塞资源。"""
            if interrupt_controller.interrupted:
                return
            print("\n\033[90m正在中断当前运行，请稍候……\033[0m", flush=True)
            interrupt_controller.interrupt_from_signal("user_cancel")

        signal.signal(signal.SIGINT, handle_interrupt)
        try:
            # 3. 上游在此期间消费事件流，模型和工具层会观察同一个中断控制器。
            yield
        finally:
            # 4. 无论运行正常、异常还是中断，都恢复输入阶段原有的 Ctrl+C 行为。
            signal.signal(signal.SIGINT, previous_handler)

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
