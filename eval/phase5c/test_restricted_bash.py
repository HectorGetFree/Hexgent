"""受限 Bash 的安全边界；所有拒绝路径均不得创建子进程。"""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from hexgent.plan import InMemoryPlanStore
from hexgent.tool.tool_context import ToolExecutionContext
from hexgent.tool.tool_execution import execute_tool_call

from .observation import project_transcript
from . import restricted_bash as restricted_bash_module
from .restricted_bash import ALLOWED_COMMAND, restricted_bash, restricted_bash_profile


class RestrictedBashTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.workspace = Path(self.temp.name)
        (self.workspace / "tests").mkdir()
        (self.workspace / "tests" / "test_ok.py").write_text(
            "import unittest\nclass Ok(unittest.TestCase):\n    def test_ok(self): self.assertTrue(True)\n",
            encoding="utf-8",
        )
        (self.workspace / "tests" / "test_slow.py").write_text(
            "import time\nimport unittest\nclass Slow(unittest.TestCase):\n    def test_waits(self): time.sleep(0.1)\n",
            encoding="utf-8",
        )
        self.context = ToolExecutionContext(InMemoryPlanStore(), self.workspace)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_exact_unittest_command_is_allowed(self) -> None:
        result = restricted_bash(ALLOWED_COMMAND, self.context, workspace=self.workspace)
        self.assertTrue(result.ok)
        self.assertEqual(result.data["exit_code"], 0)

    def test_non_allowlisted_commands_never_create_process(self) -> None:
        rejected = (
            "python -c 'print(1)'",
            "python -m unittest discover -s tests; echo unsafe",
            "python -m unittest discover -s tests | cat",
            "python -m unittest discover -s tests > result.txt",
            "$(whoami)",
            "python -m http.server",
        )
        with patch("eval.phase5c.restricted_bash.subprocess.Popen") as process:
            for command in rejected:
                result = restricted_bash(command, self.context, workspace=self.workspace)
                self.assertFalse(result.ok, command)
                self.assertEqual(result.failure.code, "eval_tool_policy_denied")
            process.assert_not_called()

    def test_other_workspace_is_denied_without_subprocess(self) -> None:
        with TemporaryDirectory() as other:
            context = ToolExecutionContext(InMemoryPlanStore(), Path(other))
            with patch("eval.phase5c.restricted_bash.subprocess.Popen") as process:
                result = restricted_bash(ALLOWED_COMMAND, context, workspace=self.workspace)
                self.assertFalse(result.ok)
                self.assertEqual(result.failure.code, "eval_tool_policy_denied")
                process.assert_not_called()

    def test_policy_denial_projects_as_existing_per_call_diagnostic(self) -> None:
        with restricted_bash_profile(self.workspace):
            result = execute_tool_call(
                "bash", json.dumps({"command": "python -c 'print(1)'"}),
                context=self.context,
                user_confirmation=lambda *_: "allow",
                call_id="restricted-call",
            )
        self.assertEqual(result.status, "denied")
        self.assertEqual(result.failure.code, "eval_tool_policy_denied")
        observation = project_transcript([
            {"type": "tool_execution_started", "payload": {"call_id": "restricted-call", "tool_name": "bash"}},
            {"type": "tool_execution_dispatched", "payload": {"call_id": "restricted-call", "tool_name": "bash"}},
            {"type": "tool_execution_finished", "payload": {
                "call_id": "restricted-call", "tool_name": "bash", "status": result.status,
                "failure": {"code": result.failure.code},
            }},
        ])
        self.assertEqual(observation.tool_calls[0].status, "denied")
        self.assertEqual(observation.tool_calls[0].error_category, "eval_tool_policy_denied")

    def test_timeout_terminates_process(self) -> None:
        with patch.object(restricted_bash_module, "_TIMEOUT_SECONDS", 0.01):
            result = restricted_bash(ALLOWED_COMMAND, self.context, workspace=self.workspace)
        self.assertFalse(result.ok)
        self.assertEqual(result.failure.code, "tool_timeout")
