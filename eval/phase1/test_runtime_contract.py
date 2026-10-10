"""已迁移场景的 Runtime Contract Eval。"""

import unittest

from eval.phase1.oracle import (
    assert_happens_before,
    assert_no_duplicate_terminal,
    assert_required_events,
    assert_terminal,
)
from eval.phase1.scenarios import v1_scenarios


class RuntimeContractScenarioTest(unittest.TestCase):
    """通过 Scenario 运行完整 Runtime，而不是直接调用内部断言对象。"""

    def test_v1_scenarios(self) -> None:
        for scenario in v1_scenarios():
            with self.subTest(scenario=scenario.name):
                observation = scenario.execute()
                assert_required_events(observation, scenario.required_events)
                assert_happens_before(observation, scenario.ordering)
                if scenario.expected_status is not None:
                    assert_terminal(observation, scenario.expected_status)
                    assert_no_duplicate_terminal(observation)


if __name__ == "__main__":
    unittest.main()
