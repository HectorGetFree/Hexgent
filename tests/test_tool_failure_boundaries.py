"""工具失败分类和业务失败语义测试。"""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from hexgent.plan import InMemoryPlanStore
from hexgent.tool.tool_context import ToolExecutionContext
from hexgent.tool.tool_execution import execute_tool_call
from hexgent.tool.tool_registry import RegisteredTool
from hexgent.tool.tool_result import ToolHandlerResult
from hexgent.tool.tool_retry import ToolRetryLimits


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
                user_confirmation=lambda _tool, _message: "allow",
            )

        self.assertEqual(result.status, "error")
        self.assertTrue(result.is_error)
        self.assertEqual(result.failure.code, "business_failure")
        self.assertEqual(result.failure.side_effect_state, "not_started")

    def test_missing_file_is_not_retryable_external_dependency(self) -> None:
        """路径不存在是确定性资源定位失败，不能被标记为可自动重试。"""
        with TemporaryDirectory() as directory:
            context = ToolExecutionContext(
                InMemoryPlanStore(), Path(directory).resolve()
            )
            result = execute_tool_call(
                "read_file", '{"path":"missing.py"}', context=context
            )

        self.assertEqual(result.status, "error")
        self.assertEqual(result.failure.code, "resource_not_found")
        self.assertFalse(result.failure.retryable)

    def test_nonzero_bash_exit_is_business_failure(self) -> None:
        """Shell 非零退出码必须形成结构化失败而不是 success。"""
        with TemporaryDirectory() as directory:
            context = ToolExecutionContext(
                InMemoryPlanStore(), Path(directory).resolve()
            )
            result = execute_tool_call(
                "bash",
                '{"command":"exit 7"}',
                context=context,
                user_confirmation=lambda _tool, _message: "allow",
            )

        self.assertEqual(result.status, "error")
        self.assertIn("退出码为 7", result.output)
        self.assertEqual(result.failure.code, "business_failure")
        self.assertEqual(result.failure.side_effect_state, "unknown")

    def test_retry_is_disabled_by_default(self) -> None:
        """没有显式配置时，即使安全工具发生瞬时失败也只执行一次。"""
        attempts = 0

        def flaky_tool() -> ToolHandlerResult:
            nonlocal attempts
            attempts += 1
            return ToolHandlerResult.failed(
                "external_dependency_failed",
                "临时不可用",
                retryable=True,
            )

        result = self._execute_registered_tool(
            "flaky",
            RegisteredTool(flaky_tool, automatic_retry_safe=True),
        )

        self.assertEqual(attempts, 1)
        self.assertEqual(result.attempt_count, 1)
        self.assertEqual(result.failure.code, "external_dependency_failed")

    def test_safe_tool_retries_retryable_failure_when_enabled(self) -> None:
        """显式开启后，安全工具应在次数上限内从瞬时失败恢复。"""
        attempts = 0

        def flaky_tool() -> ToolHandlerResult:
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                return ToolHandlerResult.failed(
                    "external_dependency_failed",
                    "临时不可用",
                    retryable=True,
                )
            return ToolHandlerResult.success("恢复成功", {"kind": "test"})

        result = self._execute_registered_tool(
            "flaky",
            RegisteredTool(
                flaky_tool,
                automatic_retry_safe=True,
                output_schema=self._test_output_schema(),
            ),
            ToolRetryLimits(
                enabled=True,
                max_attempts=3,
                initial_backoff_seconds=0.001,
                max_backoff_seconds=0.001,
            ),
        )

        self.assertEqual(result.status, "success")
        self.assertEqual(result.output, "恢复成功")
        self.assertEqual(result.attempt_count, 3)

    def test_unknown_side_effect_is_never_retried(self) -> None:
        """副作用状态不确定时，即使错误可重试也不能再次执行。"""
        attempts = 0

        def unsafe_tool() -> ToolHandlerResult:
            nonlocal attempts
            attempts += 1
            return ToolHandlerResult.failed(
                "external_dependency_failed",
                "结果未知",
                retryable=True,
                side_effect_state="unknown",
            )

        result = self._execute_registered_tool(
            "unsafe",
            RegisteredTool(unsafe_tool, automatic_retry_safe=True),
            ToolRetryLimits(enabled=True),
        )

        self.assertEqual(attempts, 1)
        self.assertEqual(result.attempt_count, 1)
        self.assertEqual(result.failure.side_effect_state, "unknown")

    def test_tool_without_safe_declaration_is_never_retried(self) -> None:
        """工具没有显式声明自动重试安全时不能仅凭错误标记重试。"""
        attempts = 0

        def undeclared_tool() -> ToolHandlerResult:
            nonlocal attempts
            attempts += 1
            return ToolHandlerResult.failed(
                "external_dependency_failed",
                "临时不可用",
                retryable=True,
            )

        result = self._execute_registered_tool(
            "undeclared",
            RegisteredTool(undeclared_tool),
            ToolRetryLimits(enabled=True),
        )

        self.assertEqual(attempts, 1)
        self.assertEqual(result.attempt_count, 1)

    @patch("hexgent.tool.tool_execution.sleep")
    def test_rate_limit_honors_retry_after(self, mocked_sleep) -> None:
        """限流失败应优先遵守外部依赖提供的最短等待时间。"""
        attempts = 0

        def rate_limited_tool() -> ToolHandlerResult:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return ToolHandlerResult.failed(
                    "rate_limited",
                    "请求过于频繁",
                    retryable=True,
                    retry_after_seconds=1.5,
                )
            return ToolHandlerResult.success("恢复成功", {"kind": "test"})

        result = self._execute_registered_tool(
            "rate_limited",
            RegisteredTool(
                rate_limited_tool,
                automatic_retry_safe=True,
                output_schema=self._test_output_schema(),
            ),
            ToolRetryLimits(
                enabled=True,
                max_attempts=2,
                initial_backoff_seconds=0.1,
            ),
        )

        self.assertEqual(result.status, "success")
        self.assertEqual(result.attempt_count, 2)
        mocked_sleep.assert_called_once_with(1.5)

    def test_output_schema_failure_is_protocol_failure(self) -> None:
        """业务成功但 JSON 数据不符合 Schema 时应归类为输出协议失败。"""
        def malformed_tool() -> ToolHandlerResult:
            return ToolHandlerResult.success("表面成功", {"kind": 123})

        result = self._execute_registered_tool(
            "malformed",
            RegisteredTool(
                malformed_tool,
                output_schema=self._test_output_schema(),
            ),
        )

        self.assertEqual(result.status, "error")
        self.assertEqual(result.failure.code, "invalid_tool_output")
        self.assertEqual(result.failure.phase, "output_validation")
        self.assertFalse(result.failure.retryable)

    def test_tool_output_validator_runs_after_schema(self) -> None:
        """工具专属 validator 应在通用 JSON Schema 通过后校验业务不变量。"""
        def valid_shape_tool() -> ToolHandlerResult:
            return ToolHandlerResult.success("表面成功", {"kind": "unexpected"})

        result = self._execute_registered_tool(
            "validated",
            RegisteredTool(
                valid_shape_tool,
                output_schema=self._test_output_schema(),
                output_validator=lambda data: (
                    None if data["kind"] == "expected" else "kind 语义无效"
                ),
            ),
        )

        self.assertEqual(result.failure.code, "invalid_tool_output")
        self.assertIn("kind 语义无效", result.output)

    def test_plain_string_result_is_protocol_failure(self) -> None:
        """未使用 ToolHandlerResult 的工具实现应被识别为协议错误。"""
        def legacy_tool():
            return "旧字符串结果"

        result = self._execute_registered_tool(
            "legacy",
            RegisteredTool(  # type: ignore[arg-type]
                legacy_tool,
                output_schema=self._test_output_schema(),
            ),
        )

        self.assertEqual(result.failure.code, "invalid_tool_output")
        self.assertIn("必须是 ToolHandlerResult", result.output)

    def _execute_registered_tool(
        self,
        tool_name: str,
        registered_tool: RegisteredTool,
        retry_limits: ToolRetryLimits | None = None,
    ):
        """用最小 Schema 执行测试注册工具。"""
        schema = {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        }
        with TemporaryDirectory() as directory, patch(
            "hexgent.tool.tool_execution.get_tool",
            return_value=registered_tool,
        ), patch.dict(
            "hexgent.tool.tool_validation.TOOL_SCHEMAS",
            {tool_name: schema},
        ):
            context = ToolExecutionContext(
                InMemoryPlanStore(), Path(directory).resolve()
            )
            return execute_tool_call(
                tool_name,
                "{}",
                context=context,
                retry_limits=retry_limits,
                user_confirmation=lambda _tool, _message: "allow",
            )

    @staticmethod
    def _test_output_schema() -> dict[str, object]:
        """返回测试工具使用的最小成功输出 Schema。"""
        return {
            "type": "object",
            "properties": {"kind": {"type": "string"}},
            "required": ["kind"],
            "additionalProperties": False,
        }


if __name__ == "__main__":
    unittest.main()
