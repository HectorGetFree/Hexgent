"""模块 Benchmark 自身的确定性回归；不调用真实模型。"""

from pathlib import Path
import json
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from .runner import existing_tests, historical_evidence, protected_hashes
from .scenarios import (
    compaction_case, context_record_continuation, parallel_case, percentile,
    recovery, safety, workspace_memory,
)


class ModuleBenchmarkTest(unittest.TestCase):
    """测试以真实输出和 oracle 为依据，而不是只比较串并行错误文本相同。"""

    def test_percentile_nearest_rank(self):
        self.assertEqual(percentile([7, 1, 3, 2, 6, 4, 5], .95), 7)
        self.assertEqual(percentile([7, 1, 3, 2, 6, 4, 5], .5), 4)

    def test_parallel_actual_execution_pairing_and_cap(self):
        result = parallel_case(4, 5, 2, staggered=True, repeats=2)
        self.assertTrue(result["call_result_pairing_correct"])
        self.assertEqual(len(result["samples"]), 4)
        for sample in result["samples"]:
            self.assertEqual(len(sample["completion_order"]), 4)
            self.assertTrue(sample["concurrency_limit_respected"])
            self.assertEqual(sample["peak_concurrency"], 2 if sample["variant"] == "parallel" else 1)
        self.assertTrue(any(s["completion_order"] != [f"call-{i}" for i in range(4)]
                            for s in result["samples"] if s["variant"] == "parallel"))

    def test_memory_explicit_write_and_new_session_injection(self):
        for row in workspace_memory():
            self.assertNotEqual(row["session_a"], row["session_b"])
            self.assertTrue(row["explicit_write_success"])
            self.assertTrue(row["recall_at_4"])
            self.assertTrue(row["new_session_context_injection"])
            self.assertTrue(row["metadata_update_same_identity"])
            self.assertEqual(row["workspace_isolation_violations"], 0)

    def test_context_record_lifecycle_after_compaction(self):
        row = context_record_continuation()
        self.assertTrue(row["qualified"])
        self.assertTrue(row["oracle_pass"])
        self.assertEqual({r["status"] for r in row["records"]}, {"active", "superseded"})

    def test_compaction_six_oracles(self):
        for name in ("long_history", "multi_read", "large_result", "active_plan", "near_budget", "complete_rounds"):
            with self.subTest(name=name):
                row = compaction_case(name)
                self.assertTrue(row["oracle_pass"], row["checks"])
                self.assertGreater(row["compression_ratio"], 0)
                self.assertLess(row["compression_ratio"], 1)

    def test_snapshot_preserves_fact_when_summary_drops_it(self):
        from . import scenarios
        original = scenarios.BenchmarkResponses
        with patch.object(scenarios, "BenchmarkResponses", side_effect=lambda *a, **k: original([], summary="事实已丢失")):
            row = compaction_case("long_history")
        self.assertTrue(row["checks"]["required_fact_retention"])
        self.assertTrue(row["oracle_pass"])

    def test_compaction_oracle_rejects_dropped_fact(self):
        from . import scenarios
        from tests.support.fact_extraction_fixture import extraction_event
        original = scenarios.BenchmarkResponses

        def omit_critical_fact(owner, request):
            stream = extraction_event(owner, request)
            if stream is None:
                return None
            event = next(stream)
            snapshot = json.loads(event.response.output_text)
            # 保留同一历史中有依据的非关键事实，模拟两个压缩通道都遗漏目标事实。
            payload = json.loads(request["input"][0]["content"])
            self.assertIn("历史信息。", str(payload["history_prefix"]))
            for fact in snapshot["facts"]:
                fact["content"] = "历史信息。"
            event.response.output_text = json.dumps(snapshot, ensure_ascii=False)
            return iter([event])

        with patch.object(scenarios, "BenchmarkResponses", side_effect=lambda *a, **k: original([], summary="事实已丢失")), \
                patch("tests.support.fact_extraction_fixture.extraction_event", side_effect=omit_critical_fact):
            row = compaction_case("long_history")
        self.assertTrue(row["checks"]["compaction_commit_success"])
        self.assertFalse(row["checks"]["required_fact_retention"])
        self.assertFalse(row["oracle_pass"])

    def test_existing_recovery_and_safety_ledgers(self):
        self.assertTrue(all(r["oracle_pass"] and r["transcript_consistent"] for r in recovery()))
        for row in safety():
            self.assertTrue(row["oracle_pass"])
            self.assertEqual(row["unauthorized_dispatch_count"], 0)
            self.assertEqual(row["unexpected_duplicate_dispatch_count"], 0)

    def test_test_discovery_reports_failure_and_skip(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            # 仅创建临时测试 fixture，不改动任何既有测试。
            (root / "test_module_fixture.py").write_text(
                "import unittest\nclass Fixture(unittest.TestCase):\n"
                " def test_fail(self): self.fail('fixture failure')\n"
                " @unittest.skip('fixture skip')\n def test_skip(self): pass\n", encoding="utf-8")
            with patch("eval.modules_v1.runner.ROOT", root):
                result = existing_tests(["test_module_fixture.py"], ".")
            self.assertEqual((result["total"], result["passed"], result["failures"], result["skipped"]), (2, 0, 1, 1))

    def test_historical_evidence_preserves_limits(self):
        report = historical_evidence()
        if report["phase5c"]["status"] != "VERIFIED_FROM_ARTIFACT":
            self.skipTest("历史 artifact 不在当前 checkout")
        self.assertLessEqual(report["phase5c"]["qualified_task_count"], report["phase5c"]["implemented_candidate_task_count"])
        self.assertIn("field presence only", report["phase5c"]["audit_field_coverage"]["scope"])
        self.assertIn("hexgent/agent_engine.py", protected_hashes())
