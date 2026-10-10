"""第二阶段：工具执行协议、权限和结果治理评测。"""

from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from hexgent.plan import InMemoryPlanStore
from hexgent.session import SessionStore, ToolResultLimits
from hexgent.session.tool_call_recovery import (
    build_recovery_tool_output,
    find_unclosed_tool_calls,
)
from hexgent.tool.tool_context import ToolExecutionContext
from hexgent.tool.tool_execution import execute_tool_call
from hexgent.tool.tool_permission_pipeline import PermissionContext, evaluate_permission
from hexgent.tool.tool_registry import get_tool
from hexgent.tool.tool_result import ToolHandlerResult
from hexgent.tool.tool_retry import ToolRetryLimits
from hexgent.tool import tool_validation


class ToolGovernanceEvaluation(unittest.TestCase):
    """验证工具结果从准备到恢复的治理边界。"""

    def test_unknown_tool_and_invalid_arguments_are_structured(self) -> None:
        with TemporaryDirectory() as directory:
            context = ToolExecutionContext(InMemoryPlanStore(), Path(directory))
            unknown = execute_tool_call("missing_tool", "{}", context=context)
            invalid = execute_tool_call(
                "read_file", '{"path":"a","line_count":0}', context=context
            )
        self.assertEqual(unknown.status, "invalid")
        self.assertEqual(unknown.failure.code, "unknown_tool")
        self.assertEqual(unknown.failure.phase, "lookup")
        self.assertEqual(invalid.status, "invalid")
        self.assertEqual(invalid.failure.code, "invalid_arguments")
        self.assertEqual(invalid.failure.side_effect_state, "not_started")

    def test_permission_decision_is_before_execution(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            context = ToolExecutionContext(InMemoryPlanStore(), root)
            tool = get_tool("read_file")
            assert tool is not None
            evaluation = evaluate_permission(
                "read_file", tool, {"path": "../outside.txt"}, context,
                PermissionContext("default"),
            )
            result = execute_tool_call(
                "read_file", '{"path":"../outside.txt"}', context=context,
                user_confirmation=lambda _tool, _message: "deny",
            )
        self.assertEqual(evaluation.behavior, "ask")
        self.assertEqual(result.status, "denied")
        self.assertEqual(result.failure.code, "user_denied")
        self.assertEqual(result.failure.side_effect_state, "not_started")

    def test_unavailable_approval_never_becomes_allow(self) -> None:
        with TemporaryDirectory() as directory:
            context = ToolExecutionContext(InMemoryPlanStore(), Path(directory))
            result = execute_tool_call(
                "read_file", '{"path":"../outside.txt"}', context=context,
                user_confirmation=lambda _tool, _message: "unavailable",
            )
        self.assertEqual(result.status, "error")
        self.assertEqual(result.failure.code, "confirmation_unavailable")
        self.assertEqual(result.failure.phase, "permission")

    def test_safe_retry_is_bounded_and_unknown_side_effect_is_not_retried(self) -> None:
        attempts = 0

        def flaky() -> ToolHandlerResult:
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                return ToolHandlerResult.failed("temporary", "暂时失败", retryable=True)
            return ToolHandlerResult.success("成功", {"ok": True})

        # 使用现有执行器的测试注册入口，避免评测依赖真实网络或进程。
        from hexgent.tool.tool_registry import RegisteredTool
        from unittest.mock import patch

        registered = RegisteredTool(flaky, automatic_retry_safe=True)
        schema = {"type": "object", "properties": {}, "required": [], "additionalProperties": False}
        with patch("hexgent.tool.tool_execution.get_tool", return_value=registered), patch.dict(
            tool_validation.TOOL_SCHEMAS, {"flaky": schema}
        ):
            result = execute_tool_call(
                "flaky", "{}",
                context=ToolExecutionContext(InMemoryPlanStore(), Path.cwd()),
                user_confirmation=lambda *_: "allow",
                retry_limits=ToolRetryLimits(enabled=True, max_attempts=3, initial_backoff_seconds=0, max_backoff_seconds=0),
            )
        self.assertEqual(result.status, "success")
        self.assertEqual(result.attempt_count, 3)

        unsafe_attempts = 0

        def unsafe() -> ToolHandlerResult:
            nonlocal unsafe_attempts
            unsafe_attempts += 1
            return ToolHandlerResult.failed("unknown", "副作用未知", retryable=True, side_effect_state="unknown")

        registered = RegisteredTool(unsafe, automatic_retry_safe=True)
        with patch("hexgent.tool.tool_execution.get_tool", return_value=registered), patch.dict(
            tool_validation.TOOL_SCHEMAS, {"unsafe": schema}
        ):
            result = execute_tool_call(
                "unsafe", "{}",
                context=ToolExecutionContext(InMemoryPlanStore(), Path.cwd()),
                user_confirmation=lambda *_: "allow",
                retry_limits=ToolRetryLimits(enabled=True, max_attempts=3, initial_backoff_seconds=0, max_backoff_seconds=0),
            )
        self.assertEqual(unsafe_attempts, 1)
        self.assertEqual(result.failure.side_effect_state, "unknown")

    def test_large_result_is_externalized_and_deduplicated(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = SessionStore(
                root,
                session_id="phase2-session",
                tool_result_limits=ToolResultLimits(max_inline_characters=10, preview_characters=5),
            )
            output = "0123456789完整工具结果"
            first = store.persist_tool_result("call-1", "read_file", output, force=True, description="read_file(a.txt)")
            second = store.persist_tool_result("call-2", "read_file", output, force=True, description="read_file(b.txt)")
            assert first is not None and second is not None
            self.assertEqual(first.artifact_id, sha256(output.encode()).hexdigest())
            self.assertEqual(first.relative_path, second.relative_path)
            self.assertEqual((root / first.relative_path).read_text(), output)
            records = store.read_transcript()
            self.assertEqual([r["type"] for r in records], ["tool_result_persisted", "tool_result_persisted"])
            self.assertTrue(all(r["payload"]["artifact_id"] == first.artifact_id for r in records))

    def test_unclosed_calls_produce_conservative_recovery_output(self) -> None:
        records = [
            {"type": "tool_execution_started", "payload": {"call_id": "read-1", "tool_name": "read_file"}},
            {"type": "tool_execution_dispatched", "payload": {"call_id": "read-1", "tool_name": "read_file", "recovery_action": "safe_to_retry"}},
            {"type": "tool_execution_started", "payload": {"call_id": "write-1", "tool_name": "write_file"}},
        ]
        unresolved = find_unclosed_tool_calls(records, {"read-1", "write-1"})
        self.assertEqual([item.recovery_action for item in unresolved], ["safe_to_retry", "not_started"])
        messages = [build_recovery_tool_output(item)["output"] for item in unresolved]
        self.assertIn("可以按需重新调用", messages[0])
        self.assertIn("本次调用未执行", messages[1])

    def test_result_failure_metadata_preserves_phase_and_side_effect(self) -> None:
        with TemporaryDirectory() as directory:
            context = ToolExecutionContext(InMemoryPlanStore(), Path(directory))
            result = execute_tool_call("bash", '{"command":"exit 7"}', context=context, user_confirmation=lambda *_: "allow")
        self.assertEqual(result.status, "error")
        self.assertEqual(result.failure.code, "business_failure")
        self.assertEqual(result.failure.phase, "execution")
        self.assertEqual(result.failure.side_effect_state, "unknown")


if __name__ == "__main__":
    unittest.main()
