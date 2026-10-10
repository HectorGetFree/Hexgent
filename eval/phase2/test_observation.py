"""Phase2 observation 的只读投影测试。"""

import unittest

from eval.phase1.observation import RuntimeObservation
from eval.phase2.observation import tool_permission_observation
from hexgent.events import ToolFinishedEvent, ToolStartedEvent


class ToolPermissionObservationTest(unittest.TestCase):
    def test_reads_tool_events_and_context_outputs_without_new_state(self) -> None:
        observation = tool_permission_observation(
            RuntimeObservation.collect(
                [
                    ToolStartedEvent("read_file", "call-1", "read_file(a.txt)"),
                    ToolFinishedEvent("read_file", "call-1", "success"),
                ],
                transcript=[
                    {
                        "type": "context_messages_appended",
                        "payload": {
                            "messages": [
                                {
                                    "type": "function_call_output",
                                    "call_id": "call-1",
                                    "output": "内容",
                                }
                            ]
                        },
                    }
                ],
            )
        )
        self.assertEqual(observation.observed_call_ids, ("call-1",))
        self.assertEqual(observation.tool_finished_events[0].status, "success")
        self.assertEqual(len(observation.function_call_outputs("call-1")), 1)


if __name__ == "__main__":
    unittest.main()
