"""Phase3 crash consistency 场景验证。"""

import unittest

from eval.phase3.runner import run_scenarios
from eval.phase3.scenarios import v1_scenarios


class RecoveryContractScenarioTest(unittest.TestCase):
    def test_crash_matrix_reports_current_semantics(self) -> None:
        report = run_scenarios()
        self.assertEqual(len(report.scenario_reports), 4)
        self.assertEqual(report.passed, 4)
        self.assertEqual(report.failed, 0)
        self.assertEqual(report.recovery_gaps, 0)
        repaired = next(
            item
            for item in report.scenario_reports
            if item.name == "finished_before_function_output"
        )
        self.assertEqual(repaired.outcome, "PASS")
        self.assertEqual(repaired.recovery_action, ("restore_finished_output",))

    def test_scenario_expectations_match_observed_recovery_behavior(self) -> None:
        for scenario in v1_scenarios():
            with self.subTest(scenario=scenario.name):
                observation = scenario.execute()
                scenario.verify(observation)


if __name__ == "__main__":
    unittest.main()
