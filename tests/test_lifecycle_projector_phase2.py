import unittest

from hexgent.integration.lifecycle import LifecycleProjector


class LifecycleProjectorPhase2Test(unittest.TestCase):
    def test_failed_run_is_deterministic_and_run_scoped(self):
        records = [
            {"sequence": 1, "type": "runtime_command_decided", "created_at": "2026-01-01T00:00:00Z",
             "payload": {"command_id": "c1", "workspace_run_id": "R1", "command_type": "StartRun", "decision": "ACCEPTED"}},
            {"sequence": 2, "type": "agent_run_started", "created_at": "2026-01-01T00:00:01Z",
             "payload": {"run_id": "A", "workspace_run_id": "R1", "conversation_id": "C1"}},
            {"sequence": 3, "type": "agent_run_finished", "created_at": "2026-01-01T00:00:02Z",
             "payload": {"run_id": "A", "workspace_run_id": "R1", "conversation_id": "C1", "status": "failed",
                         "stop_reason": "stream_error", "output_text": "model error"}},
            {"sequence": 4, "type": "runtime_command_decided", "created_at": "2026-01-01T00:00:03Z",
             "payload": {"command_id": "c2", "workspace_run_id": "R1", "command_type": "ResumeRun", "decision": "ACCEPTED"}},
            {"sequence": 5, "type": "agent_run_started", "created_at": "2026-01-01T00:00:04Z",
             "payload": {"run_id": "B", "workspace_run_id": "R1", "conversation_id": "C1"}},
        ]
        projector = LifecycleProjector()
        first = projector.project("S1", records)
        second = projector.project("S1", records)
        self.assertEqual(["AgentRunStarted", "AgentRunFailed", "AgentRunResumed"], [e.type for e in first])
        self.assertEqual([1, 2, 3], [e.sequence for e in first])
        self.assertEqual([e.event_id for e in first], [e.event_id for e in second])
        self.assertEqual("MODEL_ERROR", first[1].payload["failureCode"])
        self.assertEqual("B", first[2].payload["internalRunId"])


if __name__ == "__main__":
    unittest.main()
