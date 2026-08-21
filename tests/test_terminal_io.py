"""统一终端输入边界测试。"""

from io import StringIO
import unittest
from unittest.mock import patch

from hexgent.terminal_io import TerminalIO


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


if __name__ == "__main__":
    unittest.main()
