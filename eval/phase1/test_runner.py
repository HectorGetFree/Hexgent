"""Runner、Report 和 semantic coverage 测试。"""

import unittest

from eval.phase1.runner import run_scenarios


class RuntimeContractRunnerTest(unittest.TestCase):
    def test_v1_runner_produces_structured_report_and_semantic_coverage(self) -> None:
        report = run_scenarios()
        self.assertEqual(report.total, 6)
        self.assertEqual(report.passed, 6)
        self.assertEqual(report.failed, 0)
        self.assertEqual(report.not_yet_testable, 0)
        self.assertIn("RunFinishedEvent", report.coverage.events)
        self.assertIn("completed", report.coverage.terminal_outcomes)
        self.assertIn("failed", report.coverage.terminal_outcomes)
        self.assertIn("limit_reached", report.coverage.terminal_outcomes)
        self.assertIn("budget_finalization", report.coverage.transition_reasons)
        payload = report.to_dict()
        self.assertEqual(payload["scenarios"]["passed"], 6)
        self.assertIn("coverage", payload)
        self.assertEqual(
            payload["coverage"]["state_transitions"]["availability"],
            "unavailable",
        )
        self.assertIn("Runtime Contract Eval", report.render_text())


if __name__ == "__main__":
    unittest.main()
