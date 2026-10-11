"""无变更撤销的持久化命令身份；不改变约束状态机。"""
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from hexgent.agent_engine import AgentEngine
from hexgent.config import HexgentConfig
from hexgent.session.pinned_constraints import manage_constraint, replay_constraints
from hexgent.session.session_store import SessionStore


class NoopRevokeTests(TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.engine = AgentEngine(HexgentConfig(), self.root)
        self.item = self.engine.manage_pinned_constraint("pin", "pin", text="只读")["changes"][0]
        self.args = dict(constraint_id=self.item["id"], expected_version=2)
        result = self.engine.manage_pinned_constraint("revoke", "first", constraint_id=self.item["id"], expected_version=1)
        self.assertEqual(result["changes"][0]["status"], "REVOKED")
        self.assertEqual(result["changes"][0]["version"], 2)
        self.store = self.engine.session.store

    def commit_noop(self):
        before, _ = replay_constraints(self.store.read_transcript())
        count = len(self.store.read_transcript())
        result = self.engine.manage_pinned_constraint("revoke", "noop", **self.args)
        state, commands = replay_constraints(self.store.read_transcript())
        self.assertEqual(state, before)
        self.assertEqual(len(self.store.read_transcript()), count + 1)
        self.assertEqual(result["changes"], [])
        self.assertEqual(commands["noop"], result)
        return result

    def assert_replay(self, operation, result):
        before = self.store.read_transcript()
        self.assertEqual(operation("revoke", "noop", **self.args), result)
        with self.assertRaisesRegex(ValueError, "IDEMPOTENCY_CONFLICT"):
            operation("pin", "noop", text="其他约束")
        with self.assertRaisesRegex(ValueError, "IDEMPOTENCY_CONFLICT"):
            operation("revoke", "noop", constraint_id=self.item["id"], expected_version=1)
        self.assertEqual(self.store.read_transcript(), before)

    def test_noop_is_durable_and_repeated_command_is_immutable(self):
        self.assert_replay(self.engine.manage_pinned_constraint, self.commit_noop())

    def test_recreated_store_and_formal_restore_replay_identity(self):
        result = self.commit_noop()
        rebuilt = SessionStore.open_existing(self.root, self.store.session_id)
        self.assert_replay(lambda action, command, **args: manage_constraint(rebuilt, action, command, **args), result)
        restored = AgentEngine.restore(self.engine.config, self.root, self.store.session_id)
        self.assert_replay(restored.manage_pinned_constraint, result)

    def test_append_failure_does_not_commit_or_report_success(self):
        before = self.store.read_transcript()
        with patch.object(self.store, "append_transcript", side_effect=OSError("模拟追加失败")):
            with self.assertRaisesRegex(OSError, "模拟追加失败"):
                self.engine.manage_pinned_constraint("revoke", "noop", **self.args)
        self.assertEqual(self.store.read_transcript(), before)
        self.assertNotIn("noop", replay_constraints(before)[1])
        self.commit_noop()

    def test_other_process_denied_then_replays_single_commit(self):
        code = "from pathlib import Path; import sys; from hexgent.session.session_store import SessionStore; from hexgent.session.pinned_constraints import manage_constraint; s=SessionStore.open_existing(Path(sys.argv[1]),sys.argv[2]); manage_constraint(s,'revoke','noop',constraint_id=sys.argv[3],expected_version=2)"
        argv = [sys.executable, "-c", code, str(self.root), self.store.session_id, self.item["id"]]
        with self.store.write_guard():
            self.commit_noop()
            denied = subprocess.run(argv, capture_output=True, text=True, timeout=10)
        self.assertNotEqual(denied.returncode, 0)
        self.assertIn("SESSION_BUSY", denied.stderr)
        allowed = subprocess.run(argv, capture_output=True, text=True, timeout=10)
        self.assertEqual(allowed.returncode, 0, allowed.stderr)
        commits = [e for e in self.store.read_transcript() if e["type"] == "pinned_constraint_committed" and e["payload"]["command_id"] == "noop"]
        self.assertEqual(len(commits), 1)
