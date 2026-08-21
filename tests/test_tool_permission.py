"""工具权限决策和用户确认测试。"""

from pathlib import Path
import unittest

from hexgent.task import InMemoryTaskStore
from hexgent.tool.tool_context import ToolExecutionContext
from hexgent.tool.tool_execution import execute_tool_call
from hexgent.tool.tool_permission import check_permission


class ToolPermissionTest(unittest.TestCase):
    """验证工作区边界和三态用户确认。"""

    def setUp(self) -> None:
        """创建具有明确工作区的工具上下文。"""
        self.workdir = Path("/tmp/hexgent-workspace").resolve()
        self.context = ToolExecutionContext(InMemoryTaskStore(), self.workdir)

    def test_relative_path_is_resolved_from_context_workdir(self) -> None:
        """工作区内的相对路径应直接允许。"""
        decision = check_permission(
            "read_file",
            {"path": "tests/example.py"},
            self.context,
        )

        self.assertEqual(decision.behavior, "allow")

    def test_parent_path_requires_confirmation_and_shows_boundaries(self) -> None:
        """越过工作区的路径应请求确认并展示解析结果。"""
        decision = check_permission(
            "read_file",
            {"path": "../tests/example.py"},
            self.context,
        )

        self.assertEqual(decision.behavior, "ask")
        self.assertIn("/tmp/tests/example.py", decision.message)
        self.assertIn(str(self.workdir), decision.message)

    def test_unavailable_confirmation_returns_execution_error(self) -> None:
        """无法确认权限时应停止执行并返回环境错误。"""
        result = execute_tool_call(
            "read_file",
            '{"path":"../tests/example.py"}',
            context=self.context,
            user_confirmation=lambda _tool, _message: "unavailable",
        )

        self.assertEqual(result.status, "error")
        self.assertIn("无法读取权限确认", result.output)

    def test_explicit_denial_remains_denied(self) -> None:
        """用户明确拒绝时应保持 denied 状态。"""
        result = execute_tool_call(
            "read_file",
            '{"path":"../tests/example.py"}',
            context=self.context,
            user_confirmation=lambda _tool, _message: "deny",
        )

        self.assertEqual(result.status, "denied")
        self.assertIn("用户拒绝", result.output)


if __name__ == "__main__":
    unittest.main()
