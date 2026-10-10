"""Phase2 Tool / Permission Oracle 的负向边界测试。"""

import unittest

from eval.phase1.observation import RuntimeObservation
from eval.phase2.observation import tool_permission_observation
from eval.phase2.oracle import (
    check_denied_calls_are_not_dispatched,
    check_function_output_pairing,
    check_permission_gates_dispatch,
)
from hexgent.events import ToolFinishedEvent, ToolStartedEvent


class ToolPermissionOracleTest(unittest.TestCase):
    def _observation(self, transcript: list[dict[str, object]]):
        return tool_permission_observation(
            RuntimeObservation.collect(
                [
                    ToolStartedEvent("write_file", "call-1", "write_file(a.txt)"),
                    ToolFinishedEvent("write_file", "call-1", "denied"),
                ],
                transcript=transcript,
            )
        )

    def test_denied_call_cannot_be_dispatched(self) -> None:
        observation = self._observation(
            [
                {
                    "type": "permission_resolved",
                    "payload": {"call_id": "call-1", "behavior": "deny"},
                },
                {
                    "type": "tool_execution_dispatched",
                    "payload": {"call_id": "call-1"},
                },
            ]
        )
        self.assertEqual(len(check_denied_calls_are_not_dispatched(observation)), 1)

    def test_dispatch_requires_prior_allow_resolution(self) -> None:
        observation = self._observation(
            [
                {
                    "type": "tool_execution_dispatched",
                    "payload": {"call_id": "call-1"},
                },
                {
                    "type": "permission_resolved",
                    "payload": {"call_id": "call-1", "behavior": "allow"},
                },
            ]
        )
        self.assertTrue(check_permission_gates_dispatch(observation))

    def test_finished_call_requires_one_function_output(self) -> None:
        observation = self._observation([])
        self.assertEqual(len(check_function_output_pairing(observation)), 1)


if __name__ == "__main__":
    unittest.main()
