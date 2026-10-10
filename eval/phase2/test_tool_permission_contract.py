"""Phase2 场景通过正式 Agent Loop 的端到端契约测试。"""

import unittest

from eval.phase1.oracle import assert_terminal
from eval.phase2.oracle import check_all_contracts
from eval.phase2.scenarios import v1_scenarios


class ToolPermissionScenarioTest(unittest.TestCase):
    def test_v1_scenarios(self) -> None:
        for scenario in v1_scenarios():
            with self.subTest(scenario=scenario.name):
                observation = scenario.execute()
                assert_terminal(observation.runtime, scenario.expected_status)
                self.assertEqual(check_all_contracts(observation), ())
                scenario.verify(observation)


if __name__ == "__main__":
    unittest.main()
