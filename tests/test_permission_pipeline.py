"""权限规则、Session Mode 与审计记录测试。"""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from hexgent.plan import InMemoryPlanStore
from hexgent.session import SessionStore
from hexgent.tool.tool_permission_pipeline import PermissionContext, evaluate_permission
from hexgent.tool.tool_context import ToolExecutionContext
from hexgent.tool.tool_execution import execute_tool_batch, execute_tool_call
from hexgent.tool.tool_registry import get_tool


class PermissionPipelineTest(unittest.TestCase):
    """验证规则优先级、权限模式和用户确认边界。"""

    def _evaluate(
        self,
        tool_name: str,
        arguments: dict[str, object],
        mode: str = "default",
    ):
        """在临时工作区内评估一次真实注册工具。"""
        root = Path(self.directory).resolve()
        context = ToolExecutionContext(InMemoryPlanStore(), root)
        tool = get_tool(tool_name)
        assert tool is not None
        return evaluate_permission(
            tool_name,
            tool,
            arguments,
            context,
            PermissionContext(mode),  # type: ignore[arg-type]
        )

    def setUp(self) -> None:
        """创建每个测试独立使用的工作目录。"""
        self.temporary_directory = TemporaryDirectory()
        self.directory = self.temporary_directory.name

    def tearDown(self) -> None:
        """清理测试工作目录。"""
        self.temporary_directory.cleanup()

    def test_workspace_read_is_allowed_by_explicit_rule(self) -> None:
        """工作区内只读操作应由明确 allow 规则批准。"""
        decision = self._evaluate("read_file", {"path": "a.txt"})

        self.assertEqual(decision.behavior, "allow")
        self.assertEqual(decision.source, "allow_rule")
        self.assertEqual(decision.rule_id, "workspace_read")

    def test_default_mode_asks_for_workspace_write(self) -> None:
        """默认模式下未预批准的写操作应询问用户。"""
        decision = self._evaluate("write_file", {"path": "a.txt"})

        self.assertEqual(decision.behavior, "ask")
        self.assertEqual(decision.source, "default_fallback")

    def test_accept_edits_mode_allows_workspace_write(self) -> None:
        """自动编辑模式应批准工作区内写操作。"""
        decision = self._evaluate(
            "write_file",
            {"path": "a.txt"},
            "accept_edits",
        )

        self.assertEqual(decision.behavior, "allow")
        self.assertEqual(decision.source, "permission_mode")

    def test_plan_mode_denies_write_and_shell(self) -> None:
        """Plan 模式应阻止写文件和 Shell，同时保留工作区读取。"""
        write_decision = self._evaluate("write_file", {"path": "a.txt"}, "plan")
        shell_decision = self._evaluate("bash", {"command": "pwd"}, "plan")
        read_decision = self._evaluate("read_file", {"path": "a.txt"}, "plan")

        self.assertEqual(write_decision.behavior, "deny")
        self.assertEqual(shell_decision.behavior, "deny")
        self.assertEqual(read_decision.behavior, "allow")

    def test_dont_ask_denies_unresolved_and_explicit_ask_calls(self) -> None:
        """无交互模式应拒绝未预批准调用及命中 ask 规则的调用。"""
        unresolved = self._evaluate("bash", {"command": "pwd"}, "dont_ask")
        outside = self._evaluate(
            "read_file",
            {"path": "../outside.txt"},
            "dont_ask",
        )

        self.assertEqual(unresolved.behavior, "deny")
        self.assertEqual(unresolved.source, "dont_ask_fallback")
        self.assertEqual(outside.behavior, "deny")
        self.assertEqual(outside.rule_id, "outside_workspace_access")

    def test_auto_approve_allows_ask_but_not_hard_deny(self) -> None:
        """本地调试自动批准只跳过 ask，不得绕过硬拒绝规则。"""
        write = self._evaluate("write_file", {"path": "a.txt"}, "auto_approve")
        blocked = self._evaluate("bash", {"command": "sudo reboot"}, "auto_approve")

        self.assertEqual(write.behavior, "allow")
        self.assertEqual(write.source, "auto_approve_mode")
        self.assertEqual(blocked.behavior, "deny")
        self.assertEqual(blocked.rule_id, "critical_bash_command")

    def test_hard_deny_overrides_permission_mode(self) -> None:
        """硬拒绝规则不能被任何自动批准模式绕过。"""
        decision = self._evaluate(
            "bash",
            {"command": "sudo reboot"},
            "accept_edits",
        )

        self.assertEqual(decision.behavior, "deny")
        self.assertEqual(decision.source, "deny_rule")
        self.assertEqual(decision.rule_id, "critical_bash_command")

    def test_permission_evaluation_and_resolution_are_persisted(self) -> None:
        """一次询问审批应把评估依据和用户结果写入 Transcript。"""
        root = Path(self.directory).resolve()
        store = SessionStore(root, session_id="permission-audit")
        context = ToolExecutionContext(InMemoryPlanStore(), root)
        tool_call = SimpleNamespace(
            name="write_file",
            arguments='{"path":"a.txt","content":"内容"}',
            call_id="call-permission",
        )

        generator = execute_tool_batch(
            [tool_call],
            context=context,
            session_store=store,
            user_confirmation=lambda _tool, _message: "deny",
            remaining_tool_calls=None,
            deadline=None,
            permission_context=PermissionContext("default"),
        )
        list(generator)
        records = [
            json.loads(line)
            for line in store.transcript_path.read_text(encoding="utf-8").splitlines()
        ]

        evaluated = next(
            record for record in records if record["type"] == "permission_evaluated"
        )
        resolved = next(
            record for record in records if record["type"] == "permission_resolved"
        )
        self.assertEqual(evaluated["payload"]["call_id"], "call-permission")
        self.assertEqual(evaluated["payload"]["source"], "default_fallback")
        self.assertEqual(resolved["payload"]["resolution"], "user_denied")

    def test_unavailable_confirmation_returns_structured_denial(self) -> None:
        """缺少交互入口时应返回结构化拒绝而不是执行写操作。"""
        root = Path(self.directory).resolve()
        context = ToolExecutionContext(InMemoryPlanStore(), root)

        result = execute_tool_call(
            "write_file",
            '{"path":"a.txt","content":"内容"}',
            context=context,
        )

        self.assertEqual(result.status, "error")
        self.assertEqual(result.failure.code, "confirmation_unavailable")
        self.assertFalse((root / "a.txt").exists())


if __name__ == "__main__":
    unittest.main()
