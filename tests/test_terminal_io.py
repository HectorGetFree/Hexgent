"""统一终端输入边界测试。"""

from io import StringIO
import unittest
from unittest.mock import patch

from hexgent.adapters.cli.terminal_io import TerminalIO
from hexgent.run_control import InterruptController, RunControl


class TerminalIOTest(unittest.TestCase):
    """验证多行消息和权限确认不会互相串行消费。"""

    def test_empty_line_submits_all_input_as_one_message(self) -> None:
        """输入内容后的空行应提交前面的完整消息。"""
        terminal_io = TerminalIO()
        with patch(
            "builtins.input",
            side_effect=[
                "请完成任务：",
                "1. 读取文件",
                "2. 总结结果",
                "",
            ],
        ) as read_input:
            message = terminal_io.read_message()

        self.assertEqual(message, "请完成任务：\n1. 读取文件\n2. 总结结果")
        self.assertNotEqual(read_input.call_args_list[0].args[0], "")
        self.assertTrue(
            all(call.args[0] == "" for call in read_input.call_args_list[1:])
        )

    def test_confirmation_uses_a_separate_prompt(self) -> None:
        """完整消息提交后，确认输入应由下一次提示单独读取。"""
        terminal_io = TerminalIO()
        with patch(
            "builtins.input",
            side_effect=["第一行", "第二行", "/send", "y"],
        ):
            message = terminal_io.read_message()
            confirmation = terminal_io.request_confirmation("read_file", "测试")

        self.assertEqual(message, "第一行\n第二行")
        self.assertEqual(confirmation, "allow")

    def test_eof_returns_unavailable_instead_of_deny(self) -> None:
        """输入流关闭时不能伪装成用户主动拒绝。"""
        terminal_io = TerminalIO()

        with (
            patch("builtins.input", side_effect=EOFError),
            patch("sys.stdout", StringIO()),
        ):
            confirmation = terminal_io.request_confirmation("read_file", "测试")

        self.assertEqual(confirmation, "unavailable")

    def test_ctrl_c_interrupts_current_run_and_restores_handler(self) -> None:
        """运行期 Ctrl+C 应中断当前运行，并在结束后恢复原处理器。"""
        terminal_io = TerminalIO()
        run_control = RunControl()
        previous_handler = object()
        installed_handlers: list[object] = []

        def install_handler(_signal_number: int, handler: object) -> None:
            installed_handlers.append(handler)

        with (
            patch(
                "hexgent.adapters.cli.terminal_io.signal.getsignal",
                return_value=previous_handler,
            ),
            patch(
                "hexgent.adapters.cli.terminal_io.signal.signal",
                side_effect=install_handler,
            ),
            patch("sys.stdout", StringIO()) as output,
        ):
            with terminal_io.capture_run_interrupt(run_control):
                installed_handlers[0](2, None)

        self.assertTrue(run_control.interrupt_controller.interrupted)
        self.assertEqual(run_control.terminal_action, "pause")
        self.assertIn("正在中断当前运行", output.getvalue())
        self.assertIs(installed_handlers[-1], previous_handler)

    def test_repeated_ctrl_c_only_reports_interrupt_once(self) -> None:
        """同一轮重复 Ctrl+C 不应重复通知或覆盖首次中断原因。"""
        terminal_io = TerminalIO()
        run_control = RunControl()
        installed_handlers: list[object] = []

        with (
            patch(
                "hexgent.adapters.cli.terminal_io.signal.getsignal",
                return_value=object(),
            ),
            patch(
                "hexgent.adapters.cli.terminal_io.signal.signal",
                side_effect=lambda _number, handler: installed_handlers.append(handler),
            ),
            patch("sys.stdout", StringIO()) as output,
        ):
            with terminal_io.capture_run_interrupt(run_control):
                handler = installed_handlers[0]
                handler(2, None)
                handler(2, None)

        self.assertEqual(output.getvalue().count("正在中断当前运行"), 1)


if __name__ == "__main__":
    unittest.main()
