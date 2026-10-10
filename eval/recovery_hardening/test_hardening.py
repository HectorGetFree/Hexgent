"""跨进程场景及外部 Oracle 的确定性回归，不运行真实模型。"""

from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from .runner import execute_case, oracle, protected_inputs
from .worker import SCENARIOS


class RecoveryHardeningTest(unittest.TestCase):
    """共享一次八场景实验，避免为每个断言重复运行整个 workload。"""

    @classmethod
    def setUpClass(cls):
        cls.directory = TemporaryDirectory()
        cls.addClassCleanup(cls.directory.cleanup)
        cls.before = protected_inputs()
        cls.cases = {name: execute_case(Path(cls.directory.name), name) for name in SCENARIOS}

    def test_all_cross_process_oracles(self):
        for name, row in self.cases.items():
            with self.subTest(name=name):
                self.assertEqual(row["oracle_status"], "PASS", row)
                self.assertNotEqual(row["process_a"]["pid"], row["process_b"]["pid"])
                self.assertTrue(all(row["checks"].values()))

    def test_same_run_continue_is_not_confused_with_new_submit(self):
        checkpoint = self.cases["safe_checkpoint"]
        self.assertEqual(checkpoint["same_run_resume"], "PASS")
        self.assertEqual(checkpoint["session_followup"], "NOT_APPLICABLE")
        for name, row in self.cases.items():
            if name != "safe_checkpoint":
                self.assertEqual(row["same_run_resume"], "BLOCKED")
                self.assertEqual(row["session_followup"], "PASS")
                self.assertEqual(row["blocker"], "当前 Session 没有可继续的 Agent Run")

    def test_handler_windows_and_actual_business_states(self):
        for name, starts, commits, state in (
            ("handler_before_effect", 0, 0, None),
            ("handler_partial_effect", 1, 0, "PARTIAL"),
            ("committed_before_finished", 1, 1, "COMMITTED"),
        ):
            row = self.cases[name]
            self.assertEqual(row["handler_entered_count"], 1)
            self.assertEqual(row["side_effect_started_count"], starts)
            self.assertEqual(row["side_effect_committed_count"], commits)
            self.assertEqual(row["business_state"], state)
            self.assertTrue(row["requires_manual_verification"])

    def test_permission_pending_and_denied_remain_unexecuted(self):
        for name in ("permission_pending", "permission_denied"):
            row = self.cases[name]
            self.assertTrue(all(row["permission_checks"].values()))
            self.assertEqual(row["handler_entered_count"], 0)
            self.assertEqual(row["side_effect_committed_count"], 0)

    def reassess(self, name, *, mutate_observation=None, append_event=False):
        """破坏实际观测的独立副本，验证 Oracle 不会仅凭固定终态放行。"""
        row = self.cases[name]
        observation = deepcopy(row["observation"])
        if mutate_observation:
            mutate_observation(observation)
        raw = b"".join((json.dumps(event) + "\n").encode() for event in row["external_ledger_before"])
        after = raw + (b'{"event":"handler_entered"}\n' if append_event else b"")
        return oracle(name, row["pre_crash_records"], observation, raw, after,
                      row["business_state"], row["business_state"])

    def test_oracle_rejects_extra_handler_execution(self):
        row = self.reassess("committed_before_finished", append_event=True)
        self.assertEqual(row["oracle_status"], "FAIL")
        self.assertFalse(row["checks"]["no_external_execution_during_restore_or_resume"])

    def test_oracle_rejects_model_not_receiving_recovered_result(self):
        row = self.reassess("finished_before_context", mutate_observation=lambda obs: obs["model_requests"][0].update(input=[]))
        self.assertEqual(row["oracle_status"], "FAIL")
        self.assertFalse(row["checks"]["model_actually_received_recovered_output"])

    def test_oracle_rejects_permission_becoming_allow(self):
        def inject_allow(observation):
            observation["final_records"].append({"type": "permission_resolved", "payload": {"behavior": "allow"}})
        row = self.reassess("permission_pending", mutate_observation=inject_allow)
        self.assertEqual(row["oracle_status"], "FAIL")
        self.assertFalse(row["permission_checks"]["no_new_permission_resolution_or_allow"])

    def test_production_and_original_evidence_unchanged(self):
        self.assertEqual(self.before, protected_inputs())
