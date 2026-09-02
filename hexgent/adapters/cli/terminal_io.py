"""Hexgent 命令行的输入与控制信号边界。"""

from collections.abc import Iterator
from contextlib import contextmanager
import signal
import sys
import threading

from ...run_control import RunControl
from ...tool.tool_permission_pipeline import UserConfirmationResult


class TerminalIO:
    """把终端输入、权限确认和 Ctrl+C 转换为 Core 接口。"""

    def __init__(self) -> None:
        """初始化终端输入，并容忍粘贴内容中的非法字节。"""
        # 1. input() 在解码阶段就可能抛出异常，必须在读取前设置替换策略。
        #    合法 UTF-8 不会改变；非法字节只替换为 �，避免会话进程退出。
        try:
            if hasattr(sys.stdin, "reconfigure"):
                sys.stdin.reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            # 2. 某些测试输入或嵌入式宿主不允许重配置时，继续使用原输入流。
            pass

    @contextmanager
    def capture_run_interrupt(
        self,
        run_control: RunControl,
    ) -> Iterator[None]:
        """在当前 Agent 运行期间将 Ctrl+C 转换为本轮中断信号。"""
        # 1. 非主线程不能注册进程信号处理器，只保留程序化中断能力。
        if threading.current_thread() is not threading.main_thread():
            yield
            return

        # 2. 保存原处理器，并为当前运行临时接管 Ctrl+C。
        previous_handler = signal.getsignal(signal.SIGINT)

        def handle_interrupt(_signum: int, _frame: object) -> None:
            """立即标记本轮中断，并唤醒已经注册的阻塞资源。"""
            if run_control.interrupt_controller.interrupted:
                return
            print("\n\033[90m正在中断当前运行，请稍候……\033[0m", flush=True)
            run_control.pause_from_signal()

        signal.signal(signal.SIGINT, handle_interrupt)
        try:
            # 3. 上游消费事件流时，Core 观察同一个 RunControl。
            yield
        finally:
            # 4. 无论运行如何结束，都恢复输入阶段原有的 Ctrl+C 行为。
            signal.signal(signal.SIGINT, previous_handler)

    def read_message(self) -> str:
        """持续读取多行内容，直到输入空行或 /send 后提交。"""
        # 1. 只在第一行展示提示，避免粘贴多行时重复绘制续行符号。
        lines: list[str] = []
        while True:
            prompt = "\033[32m你：\033[0m" if not lines else ""
            line = input(prompt)

            # 2. /send 或空行结束消息，退出命令可以直接提交。
            if not lines and line.strip().lower() in {"q", "exit"}:
                return line
            if line.strip().lower() == "/send":
                return "\n".join(lines)
            if not line:
                return "\n".join(lines)
            lines.append(line)

    def select_restore_session(
        self,
        sessions: list[tuple[str, str]],
    ) -> str | None:
        """展示可恢复 Session，并读取用户选择。"""
        # 1. Session ID 是恢复的稳定标识，序号仅用于减少终端输入成本。
        print("可恢复的最近会话：")
        for index, (session_id, display_name) in enumerate(sessions, start=1):
            print(f"  {index}. {display_name}（ID：{session_id}）")

        # 2. 空输入表示取消；合法序号和完整 Session ID 均可选择。
        selection = input("输入序号或 Session ID（直接回车取消）：").strip()
        if not selection:
            return None
        if selection.isdigit():
            index = int(selection)
            if 1 <= index <= len(sessions):
                return sessions[index - 1][0]
            raise ValueError("会话序号超出可选范围")
        if selection in {session_id for session_id, _ in sessions}:
            return selection
        raise ValueError("输入的 Session ID 不在可恢复列表中")

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
