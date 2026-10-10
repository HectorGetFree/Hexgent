"""Phase 5C Eval-only per-task tool policy tests。"""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from hexgent.loop import agent_turn
from hexgent.plan import InMemoryPlanStore
from hexgent.tool.tool_context import ToolExecutionContext
from hexgent.tool.tool_execution import execute_tool_call

from .specifications import task_by_id
from .tool_exposure import task_tool_exposure, visible_tool_names


class TaskToolExposureTest(unittest.TestCase):
    def test_visible_schemas_follow_task_contract_exactly(self) -> None:
        self.assertEqual(visible_tool_names(task_by_id("R1")), ("glob", "read_file", "write_file"))
        self.assertEqual(visible_tool_names(task_by_id("W1")), (
            "glob", "read_file", "write_file", "create_plan", "update_plan",
        ))
        self.assertEqual(visible_tool_names(task_by_id("T1")), (
            "glob", "read_file", "edit_file", "write_file", "bash",
        ))
        self.assertNotIn("bash", visible_tool_names(task_by_id("D2")))
        self.assertNotIn("bash", visible_tool_names(task_by_id("L1")))

    def test_context_filters_model_schema_and_restores_it(self) -> None:
        original = agent_turn.TOOLS
        with task_tool_exposure(task_by_id("D2")):
            self.assertEqual(
                tuple(item["name"] for item in agent_turn.TOOLS),
                ("glob", "read_file", "write_file"),
            )
        self.assertIs(agent_turn.TOOLS, original)

    def test_d2_forged_bash_call_is_denied_by_eval_policy(self) -> None:
        with TemporaryDirectory() as directory:
            context = ToolExecutionContext(InMemoryPlanStore(), Path(directory))
            with patch("hexgent.tool.builtin_tools.subprocess.Popen") as process:
                with task_tool_exposure(task_by_id("D2")):
                    result = execute_tool_call(
                        "bash",
                        json.dumps({"command": "python -m unittest discover -s tests"}),
                        context=context,
                        user_confirmation=lambda *_: "allow",
                        call_id="forged-bash",
                    )
                process.assert_not_called()

        self.assertEqual(result.status, "denied")
        self.assertIsNotNone(result.failure)
        self.assertEqual(result.failure.code, "eval_tool_policy_denied")
        self.assertEqual(result.failure.phase, "permission")
