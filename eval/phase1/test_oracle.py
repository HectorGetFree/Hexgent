"""Event Contract Oracle 的行为测试。"""

import unittest

from hexgent.events import RunFinishedEvent, TextDeltaEvent
from hexgent.events import AgentLoopResult

from eval.phase1.observation import RuntimeObservation
from eval.phase1.oracle import (
    assert_happens_before,
    assert_no_runtime_effect_after_rejection,
    assert_required_events,
    assert_terminal,
    check_no_duplicate_terminal,
)


class EventContractOracleTest(unittest.TestCase):
    """验证 Oracle 检查语义而不是完整事件列表。"""

    def test_required_events_and_order_allow_unlisted_middle_events(self) -> None:
        observation = RuntimeObservation.collect(
            [
                TextDeltaEvent("处理中"),
                RunFinishedEvent(AgentLoopResult("完成", 0.1)),
            ]
        )
        assert_required_events(observation, ["TextDeltaEvent", "RunFinishedEvent"])
        assert_happens_before(observation, [("TextDeltaEvent", "RunFinishedEvent")])
        assert_terminal(observation, "completed")

    def test_terminal_oracle_reports_missing_or_duplicate_terminal(self) -> None:
        empty = RuntimeObservation.collect([])
        with self.assertRaises(AssertionError):
            assert_terminal(empty, "completed")

        duplicate = RuntimeObservation.collect(
            [
                RunFinishedEvent(AgentLoopResult("一次", 0.1)),
                RunFinishedEvent(AgentLoopResult("二次", 0.1)),
            ]
        )
        self.assertEqual(len(check_no_duplicate_terminal(duplicate)), 1)

    def test_rejected_command_cannot_have_runtime_effect(self) -> None:
        observation = RuntimeObservation.collect(
            [],
            transcript=[
                {
                    "type": "runtime_command_decided",
                    "payload": {"command_id": "cmd-1", "decision": "REJECTED"},
                },
                {
                    "type": "runtime_control_effect",
                    "payload": {"command_id": "cmd-2", "effect": "paused"},
                },
            ],
        )
        assert_no_runtime_effect_after_rejection(observation)

        invalid = RuntimeObservation.collect(
            [],
            transcript=[
                {
                    "type": "runtime_command_decided",
                    "payload": {"command_id": "cmd-1", "decision": "REJECTED"},
                },
                {
                    "type": "runtime_control_effect",
                    "payload": {"command_id": "cmd-1", "effect": "paused"},
                },
            ],
        )
        with self.assertRaises(AssertionError):
            assert_no_runtime_effect_after_rejection(invalid)


if __name__ == "__main__":
    unittest.main()
