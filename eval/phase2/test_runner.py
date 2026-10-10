"""Phase2 Runner、Report 和 observable coverage 测试。"""

import unittest

from eval.phase2.runner import run_scenarios


class ToolPermissionRunnerTest(unittest.TestCase):
    def test_v1_runner_reports_all_contract_scenarios(self) -> None:
        report = run_scenarios()
        self.assertEqual(report.total, 6)
        self.assertEqual(report.passed, 6)
        self.assertEqual(report.failed, 0)
        self.assertIn("ToolStartedEvent", report.observed_events)
        self.assertIn("success", report.observed_tool_statuses)
        self.assertIn("denied", report.observed_tool_statuses)
        self.assertIn("user_allowed", report.permission_resolutions)
        self.assertIn("user_denied", report.permission_resolutions)
        self.assertIn("policy_denied", report.permission_resolutions)
        self.assertEqual(report.to_dict()["scenarios"]["passed"], 6)
        self.assertIn("Tool / Permission Contract Eval", report.render_text())


if __name__ == "__main__":
    unittest.main()
