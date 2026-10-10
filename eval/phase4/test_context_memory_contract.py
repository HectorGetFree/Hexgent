"""Phase4 Context / Memory 评测场景验证。"""

import unittest

from eval.phase4.runner import run_scenarios
from eval.phase4.scenarios import v1_scenarios


class ContextMemoryContractScenarioTest(unittest.TestCase):
    """所有可声明 PASS 的 Phase4 场景必须实际通过 Runtime API。"""

    def test_context_memory_matrix_reports_current_scope(self) -> None:
        """正确性场景通过，工作区级 Memory 边界显式为 UNAVAILABLE。"""

        report = run_scenarios()

        self.assertEqual(report.passed, 4)
        self.assertEqual(report.failed, 0)
        self.assertEqual(report.unavailable, 1)
        scope = next(
            item for item in report.scenario_reports
            if item.name == "workspace_memory_session_isolation"
        )
        self.assertEqual(scope.outcome, "UNAVAILABLE")

    def test_scenario_expectations_match_runtime_observations(self) -> None:
        """单独重放所有可评测场景，避免 Runner 掩盖断言失败。"""

        for scenario in v1_scenarios():
            with self.subTest(scenario=scenario.name):
                observation = scenario.execute()
                scenario.verify(observation)


if __name__ == "__main__":
    unittest.main()
