"""RuntimeObservation 的最小契约测试。"""

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from hexgent.session import Session

from eval.phase1.observation import RuntimeObservation, observation_from_session
from eval.support.agent_contracts import run_trace, text_response


class RuntimeObservationTest(unittest.TestCase):
    """确认 Observation 只汇总现有 Runtime 输出，不制造新事实。"""

    def test_collect_exposes_realtime_contract_without_forcing_terminal(self) -> None:
        observation = RuntimeObservation.collect([])
        self.assertEqual(observation.event_types, ())
        self.assertIsNone(observation.terminal)
        self.assertIsNone(observation.terminal_status)
        self.assertFalse(observation.continuable)

    def test_observation_reads_transcript_and_terminal_from_one_run(self) -> None:
        with TemporaryDirectory() as directory:
            session = Session.persistent(Path(directory))
            session.record_user_query(SimpleNamespace(sequence=1, content="完成任务"))
            trace, _ = run_trace(
                [text_response("已完成")],
                session=session,
                workdir=Path(directory),
            )
            observation = observation_from_session(trace.events, session)

        self.assertEqual(observation.terminal_status, "completed")
        self.assertIn("RunFinishedEvent", observation.event_types)
        self.assertIn("agent_run_started", observation.transcript_types)
        self.assertIn("agent_run_finished", observation.transcript_types)
        self.assertIsNotNone(observation.final_run_record)
        self.assertFalse(observation.continuable)

    def test_observation_keeps_realtime_and_durable_facts_separate(self) -> None:
        observation = RuntimeObservation.collect(
            [],
            transcript=[{"type": "agent_run_finished", "payload": {"status": "failed"}}],
        )
        self.assertEqual(observation.event_types, ())
        self.assertEqual(observation.transcript_types, ("agent_run_finished",))
        self.assertIsNone(observation.terminal_status)
        self.assertEqual(observation.final_run_record["payload"]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
