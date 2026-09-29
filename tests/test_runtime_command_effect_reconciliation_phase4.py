"""验证 Phase 4 控制 effect 的 durable marker 与重启投影语义。"""

import json
import unittest
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory

from hexgent.integration.lifecycle import LifecycleProjector
from hexgent.session import Session
from hexgent.session.session_store import SessionStore
from hexgent.adapters.web.run_manager import RunManager
from hexgent.config import HexgentConfig
from hexgent.integration.protocol import RuntimeCommand
from hexgent.integration.runtime_adapter import RuntimeCommandAdapter
from hexgent.integration.protocol import CommandDecision
from tests.test_runtime_command_adapter_phase1 import _ProbeEngineFactory


class RuntimeCommandEffectReconciliationPhase4Test(unittest.TestCase):
    def test_pause_recovery_keeps_checkpoint_and_resume_creates_new_run(self):
        with TemporaryDirectory() as directory:
            root, session_id, adapter, manager = self._prepare_continuable(Path(directory))
            command = RuntimeCommand("pause-crash", "PauseRun", "R1", "C1", "2026-01-01T00:01:00Z", {})
            adapter._persist_decision(session_id, command, CommandDecision("pause-crash", "R1", "ACCEPTED"))
            del adapter, manager
            manager_b = RunManager(HexgentConfig(), root, engine_factory=_ProbeEngineFactory())
            adapter_b = RuntimeCommandAdapter(manager_b)
            asyncio.run(adapter_b.reconcile_command_effect("pause-crash"))
            state = adapter_b.get_command_state("pause-crash")
            self.assertEqual("APPLIED", state["effect"])
            self.assertEqual(["AgentRunPaused"], [event.type for event in adapter_b._lifecycle_projector.project(
                session_id, adapter_b._all_session_records()[0][1]
            ) if event.caused_by_command_id == "pause-crash"])
            resume = RuntimeCommand("resume-after-pause", "ResumeRun", "R1", "C1", "2026-01-01T00:02:00Z", {})
            async def resume_and_wait():
                await adapter_b.handle(resume)
                handle = manager_b._runs[adapter_b._manager_run_ids["R1"]]
                await asyncio.wait_for(handle.task, timeout=1)
            asyncio.run(resume_and_wait())
            started = self._started_ids(root, session_id)
            self.assertGreaterEqual(len(started), 2)
            self.assertNotEqual(started[-2], started[-1])

    def test_cancel_recovery_rejects_continue_and_resume_after_restart(self):
        with TemporaryDirectory() as directory:
            root, session_id, adapter, manager = self._prepare_continuable(Path(directory))
            command = RuntimeCommand("cancel-crash", "CancelRun", "R1", "C1", "2026-01-01T00:01:00Z", {})
            adapter._persist_decision(session_id, command, CommandDecision("cancel-crash", "R1", "ACCEPTED"))
            del adapter, manager
            manager_b = RunManager(HexgentConfig(), root, engine_factory=_ProbeEngineFactory())
            adapter_b = RuntimeCommandAdapter(manager_b)
            result = asyncio.run(adapter_b.reconcile_command_effect("cancel-crash"))
            self.assertEqual("APPLIED", result["effect"])
            restored = manager_b._require_session(session_id)
            self.assertFalse(restored.engine.has_continuable_run)
            for command_type in ("ContinueRun", "ResumeRun"):
                decision = asyncio.run(adapter_b.handle(RuntimeCommand(
                    "cancel-" + command_type, command_type, "R1", "C1",
                    "2026-01-01T00:02:00Z", {},
                )))
                self.assertEqual("REJECTED", decision.status)
            manager_c = RunManager(HexgentConfig(), root, engine_factory=_ProbeEngineFactory())
            adapter_c = RuntimeCommandAdapter(manager_c)
            for command_type in ("ContinueRun", "ResumeRun"):
                decision = asyncio.run(adapter_c.handle(RuntimeCommand(
                    "restart-cancel-" + command_type, command_type, "R1", "C1",
                    "2026-01-01T00:03:00Z", {},
                )))
                self.assertEqual("REJECTED", decision.status)

    def test_steer_case_a_restarts_and_applies_once(self):
        with TemporaryDirectory() as directory:
            root, session_id, adapter, manager = self._prepare_continuable(Path(directory))
            command = RuntimeCommand("steer-crash", "SteerRun", "R1", "C1", "2026-01-01T00:01:00Z", {
                "instruction": "只检查权限",
            })
            adapter._persist_decision(session_id, command, CommandDecision("steer-crash", "R1", "ACCEPTED"))
            del adapter, manager
            manager_b = RunManager(HexgentConfig(), root, engine_factory=_ProbeEngineFactory())
            adapter_b = RuntimeCommandAdapter(manager_b)
            asyncio.run(adapter_b.reconcile_command_effect("steer-crash"))
            async def continue_and_wait():
                await adapter_b.handle(RuntimeCommand(
                    "continue-after-steer", "ContinueRun", "R1", "C1",
                    "2026-01-01T00:02:00Z", {},
                ))
                handle = manager_b._runs[adapter_b._manager_run_ids["R1"]]
                await asyncio.wait_for(handle.task, timeout=1)
            asyncio.run(continue_and_wait())
            records = self._records(root, session_id)
            mutations = [item for item in records if item["type"] == "context_user_query_appended"
                          and item["payload"].get("command_id") == "steer-crash"]
            applied = [item for item in records if item["type"] == "runtime_control_effect"
                       and item["payload"].get("command_id") == "steer-crash"
                       and item["payload"].get("effect") == "steering_applied"]
            self.assertEqual(1, len(mutations))
            self.assertEqual(1, len(applied))
            self.assertEqual("APPLIED", adapter_b.get_command_state("steer-crash")["effect"])

    def test_concurrent_reconcile_same_command_has_one_effect(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            manager = RunManager(HexgentConfig(), root, engine_factory=_ProbeEngineFactory())
            adapter = RuntimeCommandAdapter(manager)
            session = asyncio.run(manager.create_session("default"))
            command = RuntimeCommand("concurrent-start", "StartRun", "R1", "C1",
                                    "2026-01-01T00:00:00Z", {"instruction": "开始"})
            adapter._persist_decision(session.session_id, command,
                                      CommandDecision("concurrent-start", "R1", "ACCEPTED"))
            adapter._conversation_sessions["C1"] = session.session_id
            async def run_both():
                results = await asyncio.gather(
                    adapter.reconcile_command_effect("concurrent-start"),
                    adapter.reconcile_command_effect("concurrent-start"),
                )
                handle = manager._runs[adapter._manager_run_ids["R1"]]
                await asyncio.wait_for(handle.task, timeout=1)
                return results
            results = asyncio.run(run_both())
            self.assertEqual(results[0]["commandId"], results[1]["commandId"])
            records = self._records(root, session.session_id)
            self.assertEqual(1, len([item for item in records if item["type"] == "agent_run_started"]))

    def test_rejected_and_missing_command_never_apply(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            manager = RunManager(HexgentConfig(), root, engine_factory=_ProbeEngineFactory())
            adapter = RuntimeCommandAdapter(manager)
            session = asyncio.run(manager.create_session("default"))
            adapter._conversation_sessions["C1"] = session.session_id
            rejected_command = RuntimeCommand(
                "bad-start", "StartRun", "R1", "C1", "2026-01-01T00:00:00Z", {},
            )
            rejected = asyncio.run(adapter.handle(rejected_command))
            self.assertEqual("REJECTED", rejected.status)
            rejected_result = asyncio.run(adapter.reconcile_command_effect("bad-start"))
            self.assertEqual("REJECTED", rejected_result["decision"])
            missing = asyncio.run(adapter.reconcile_command_effect("does-not-exist"))
            self.assertEqual("NOT_FOUND", missing["status"])

    def _prepare_continuable(self, root: Path):
        manager = RunManager(HexgentConfig(), root, engine_factory=_ProbeEngineFactory())
        adapter = RuntimeCommandAdapter(manager)
        async def start_and_wait():
            await adapter.handle(RuntimeCommand("prepare", "StartRun", "R1", "C1",
                                                "2026-01-01T00:00:00Z", {"instruction": "开始"}))
            handle = manager._runs[adapter._manager_run_ids["R1"]]
            await asyncio.wait_for(handle.task, timeout=1)
            return handle.session_id
        session_id = asyncio.run(start_and_wait())
        return root, session_id, adapter, manager

    def test_continue_accepted_before_apply_reconciles_after_restart(self):
        self._assert_continuation_recovery("ContinueRun", "continue-crash")

    def test_resume_accepted_before_apply_reconciles_after_restart(self):
        self._assert_continuation_recovery("ResumeRun", "resume-crash")

    def _assert_continuation_recovery(self, command_type: str, command_id: str):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            manager_a = RunManager(HexgentConfig(), root, engine_factory=_ProbeEngineFactory())
            adapter_a = RuntimeCommandAdapter(manager_a)
            async def start_and_wait():
                await adapter_a.handle(RuntimeCommand(
                    "start-before-continuation", "StartRun", "R1", "C1",
                    "2026-01-01T00:00:00Z", {"instruction": "开始"},
                ))
                first = manager_a._runs[adapter_a._manager_run_ids["R1"]]
                await asyncio.wait_for(first.task, timeout=1)
                return first
            first = asyncio.run(start_and_wait())
            session_id = first.session_id
            before_ids = self._started_ids(root, session_id)

            manager_b = RunManager(HexgentConfig(), root, engine_factory=_ProbeEngineFactory())
            adapter_b = RuntimeCommandAdapter(manager_b)
            asyncio.run(manager_b.restore_session(session_id))
            command = RuntimeCommand(
                command_id, command_type, "R1", "C1", "2026-01-01T00:00:01Z", {},
            )
            adapter_b._persist_decision(session_id, command, CommandDecision(
                command_id, "R1", "ACCEPTED",
            ))
            del adapter_b, manager_b

            manager_c = RunManager(HexgentConfig(), root, engine_factory=_ProbeEngineFactory())
            adapter_c = RuntimeCommandAdapter(manager_c)

            async def reconcile_and_wait():
                await adapter_c.reconcile_command_effect(command_id)
                handle = manager_c._runs[adapter_c._manager_run_ids["R1"]]
                await asyncio.wait_for(handle.task, timeout=1)
            asyncio.run(reconcile_and_wait())

            after_ids = self._started_ids(root, session_id)
            self.assertEqual(len(before_ids) + 1, len(after_ids))
            self.assertNotEqual(before_ids[-1], after_ids[-1])
            started = self._records(root, session_id)
            latest = [item for item in started if item["type"] == "agent_run_started"][-1]
            self.assertEqual(command_id, latest["payload"]["caused_by_command_id"])
            self.assertEqual("APPLIED", adapter_c.get_command_state(command_id)["effect"])
            asyncio.run(adapter_c.reconcile_command_effect(command_id))
            self.assertEqual(len(after_ids), len(self._started_ids(root, session_id)))

    def test_start_accepted_before_apply_reconciles_after_restart_once(self):
        """Decision 已提交但 Start 未执行时，重启后只产生一次 started fact。"""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            manager_a = RunManager(HexgentConfig(), root, engine_factory=_ProbeEngineFactory())
            adapter_a = RuntimeCommandAdapter(manager_a)
            session = asyncio.run(manager_a.create_session("default"))
            command = RuntimeCommand(
                "start-crash", "StartRun", "R1", "C1", "2026-01-01T00:00:00Z",
                {"instruction": "开始任务"},
            )
            adapter_a._persist_decision(session.session_id, command,  # type: ignore[arg-type]
                                        __import__("hexgent.integration.protocol", fromlist=["CommandDecision"]).CommandDecision(
                                            "start-crash", "R1", "ACCEPTED"))
            del adapter_a, manager_a

            manager_b = RunManager(HexgentConfig(), root, engine_factory=_ProbeEngineFactory())
            adapter_b = RuntimeCommandAdapter(manager_b)
            async def reconcile_and_wait():
                await adapter_b.reconcile_command_effect("start-crash")
                handle = manager_b._runs[adapter_b._manager_run_ids["R1"]]
                await asyncio.wait_for(handle.task, timeout=1)
            asyncio.run(reconcile_and_wait())
            result = adapter_b.get_command_state("start-crash")
            self.assertEqual("APPLIED", result["effect"])
            records = json.loads("[" + ",".join(
                (root / ".hexgent" / "sessions" / session.session_id / "transcript.jsonl").read_text(
                    encoding="utf-8").splitlines()) + "]")
            started = [record for record in records if record["type"] == "agent_run_started"]
            self.assertEqual(1, len(started))
            self.assertEqual("R1", started[0]["payload"]["workspace_run_id"])
            self.assertEqual("start-crash", started[0]["payload"]["caused_by_command_id"])
            asyncio.run(adapter_b.reconcile_command_effect("start-crash"))
            records = [json.loads(line) for line in (
                root / ".hexgent" / "sessions" / session.session_id / "transcript.jsonl"
            ).read_text(encoding="utf-8").splitlines()]
            self.assertEqual(1, len([record for record in records if record["type"] == "agent_run_started"]))

    def test_control_effect_marker_is_durable_and_idempotent(self):
        with TemporaryDirectory() as directory:
            session = Session.persistent(Path(directory))
            session.initialize_runtime_session("S1")
            self.assertTrue(session.record_runtime_control_effect(
                "cancel-1", "cancelled", {"workspace_run_id": "R1", "conversation_id": "C1"},
            ))
            self.assertFalse(session.record_runtime_control_effect(
                "cancel-1", "cancelled", {"workspace_run_id": "R1", "conversation_id": "C1"},
            ))
            records = session.store.read_transcript()
            effects = [r for r in records if r["type"] == "runtime_control_effect"]
            self.assertEqual(1, len(effects))
            self.assertTrue(session.cancellation_committed)

    @staticmethod
    def _records(root: Path, session_id: str) -> list[dict]:
        return [json.loads(line) for line in (
            root / ".hexgent" / "sessions" / session_id / "transcript.jsonl"
        ).read_text(encoding="utf-8").splitlines()]

    @classmethod
    def _started_ids(cls, root: Path, session_id: str) -> list[str]:
        return [
            item["payload"]["run_id"] for item in cls._records(root, session_id)
            if item["type"] == "agent_run_started"
        ]

    def test_restart_reads_cancel_tombstone_and_projects_cancelled(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            session = Session.persistent(root)
            session.initialize_runtime_session("S1")
            session.record_runtime_control_effect(
                "cancel-1", "cancelled", {"workspace_run_id": "R1", "conversation_id": "C1"},
            )
            restored = Session.restored(SessionStore.open_existing(root, "S1"), session.settings)
            self.assertTrue(restored.cancellation_committed)
            records = [json.loads(line) for line in (
                root / ".hexgent" / "sessions" / "S1" / "transcript.jsonl"
            ).read_text(encoding="utf-8").splitlines()]
            events = LifecycleProjector().project("S1", records)
            self.assertEqual(["AgentRunCancelled"], [event.type for event in events])
            self.assertEqual("cancel-1", events[0].caused_by_command_id)

    def test_pause_and_cancel_order_does_not_project_stale_pause(self):
        records = [
            {"sequence": 1, "type": "runtime_command_decided", "created_at": "2026-01-01T00:00:00Z",
             "payload": {"command_id": "pause-1", "command_type": "PauseRun", "decision": "ACCEPTED", "workspace_run_id": "R1"}},
            {"sequence": 2, "type": "runtime_control_effect", "created_at": "2026-01-01T00:00:01Z",
             "payload": {"command_id": "pause-1", "effect": "paused", "workspace_run_id": "R1", "conversation_id": "C1"}},
            {"sequence": 3, "type": "runtime_command_decided", "created_at": "2026-01-01T00:00:01Z",
             "payload": {"command_id": "cancel-1", "command_type": "CancelRun", "decision": "ACCEPTED", "workspace_run_id": "R1"}},
            {"sequence": 4, "type": "runtime_control_effect", "created_at": "2026-01-01T00:00:02Z",
             "payload": {"command_id": "cancel-1", "effect": "cancelled", "workspace_run_id": "R1", "conversation_id": "C1"}},
        ]
        events = LifecycleProjector().project("S1", records)
        self.assertEqual(["AgentRunCancelled"], [event.type for event in events])

    def test_steering_context_mutation_without_applied_marker_is_not_repeated_after_restart(self):
        """Context 已带 commandId 但 marker 丢失时，重启恢复不得二次注入。"""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            session = Session.persistent(root)
            session.initialize_runtime_session("S1")
            session.record_runtime_control_effect(
                "steer-1", "steering_intent", {
                    "workspace_run_id": "R1", "conversation_id": "C1", "instruction": "只检查权限",
                },
            )
            context = __import__("hexgent.context", fromlist=["ContextManager"]).ContextManager()
            query = context.append_user_query("只检查权限")
            session.record_user_query(query, source="steering", command_id="steer-1")
            del session, context

            restored = Session.restored(SessionStore.open_existing(root, "S1"),
                                        __import__("hexgent.session", fromlist=["SessionSettings"]).SessionSettings())
            self.assertEqual(1, len(restored.steering_context_mutations("steer-1")))
            self.assertEqual([], restored.pending_steering_intents())


if __name__ == "__main__":
    unittest.main()
