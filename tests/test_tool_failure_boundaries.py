"""工具失败分类和业务失败语义测试。"""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from hexgent.plan import InMemoryPlanStore
from hexgent.tool.tool_context import ToolExecutionContext
from hexgent.tool.tool_execution import execute_tool_call


class ToolFailureBoundariesTest(unittest.TestCase):
    """验证失败不再依赖输出文本前缀进行判断。"""

    def test_unknown_tool_has_lookup_failure(self) -> None:
        """未知工具应形成可供模型修正的查找失败。"""
        with TemporaryDirectory() as directory:
            context = ToolExecutionContext(
                InMemoryPlanStore(), Path(directory).resolve()
            )
            result = execute_tool_call(
                "missing_tool", "{}", context=context
            )

        self.assertEqual(result.status, "invalid")
        self.assertTrue(result.is_error)
        self.assertIsNotNone(result.failure)
        self.assertEqual(result.failure.code, "unknown_tool")
        self.assertEqual(result.failure.phase, "lookup")

    def test_invalid_arguments_have_validation_failure(self) -> None:
        """参数错误应携带 validation 阶段和稳定错误码。"""
        with TemporaryDirectory() as directory:
            context = ToolExecutionContext(
                InMemoryPlanStore(), Path(directory).resolve()
            )
            result = execute_tool_call(
                "read_file", '{"path":"a.py","line_count":0}', context=context
            )

        self.assertEqual(result.status, "invalid")
        self.assertEqual(result.failure.code, "invalid_arguments")
        self.assertEqual(result.failure.phase, "validation")
        self.assertEqual(result.failure.side_effect_state, "not_started")

    def test_user_denial_is_distinct_from_policy_denial(self) -> None:
        """用户拒绝审批应使用独立错误码，不能与策略拒绝混淆。"""
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            context = ToolExecutionContext(InMemoryPlanStore(), root)
            result = execute_tool_call(
                "read_file",
                '{"path":"../outside.py"}',
                context=context,
                user_confirmation=lambda _tool, _message: "deny",
            )

        self.assertEqual(result.status, "denied")
        self.assertEqual(result.failure.code, "user_denied")
        self.assertEqual(result.failure.phase, "permission")

    def test_edit_business_failure_is_not_reported_as_success(self) -> None:
        """未找到原文本属于业务失败，不应因 Python 正常返回而标记成功。"""
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "a.txt").write_text("原始内容", encoding="utf-8")
            context = ToolExecutionContext(InMemoryPlanStore(), root)
            result = execute_tool_call(
                "edit_file",
                '{"path":"a.txt","old_text":"不存在","new_text":"新内容"}',
                context=context,
            )

        self.assertEqual(result.status, "error")
        self.assertTrue(result.is_error)
        self.assertEqual(result.failure.code, "business_failure")
        self.assertEqual(result.failure.side_effect_state, "not_started")

    def test_nonzero_bash_exit_is_business_failure(self) -> None:
        """Shell 非零退出码必须形成结构化失败而不是 success。"""
        with TemporaryDirectory() as directory:
            context = ToolExecutionContext(
                InMemoryPlanStore(), Path(directory).resolve()
            )
            result = execute_tool_call(
                "bash", '{"command":"exit 7"}', context=context
            )

        self.assertEqual(result.status, "error")
        self.assertIn("退出码为 7", result.output)
        self.assertEqual(result.failure.code, "business_failure")
        self.assertEqual(result.failure.side_effect_state, "unknown")


if __name__ == "__main__":
    unittest.main()
