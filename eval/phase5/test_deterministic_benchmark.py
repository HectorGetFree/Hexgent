"""Phase5A deterministic benchmark 的指标与正确性约束验证。"""

import unittest
from dataclasses import asdict
from unittest.mock import patch

from eval.phase5.runner import run_benchmark


class DeterministicRuntimeEfficiencyBenchmarkTest(unittest.TestCase):
    """只验证可归因的机制数据，不从 FakeModel 推导行为或延迟收益。"""

    def test_compression_pair_only_changes_proactive_threshold(self):
        from eval.phase5 import scenarios
        original = scenarios._base_config
        configs = []

        def capture(**arguments):
            config = original(**arguments)
            configs.append(asdict(config))
            return config

        with patch.object(scenarios, "_base_config", side_effect=capture):
            result = scenarios.context_compression()
        self.assertEqual(len(configs), 2)
        enabled, baseline = configs
        self.assertNotEqual(enabled["context"]["proactive_threshold_tokens"], baseline["context"]["proactive_threshold_tokens"])
        baseline["context"]["proactive_threshold_tokens"] = enabled["context"]["proactive_threshold_tokens"]
        self.assertEqual(enabled, baseline)
        self.assertEqual(enabled["context"]["reserved_output_tokens"], enabled["loop"]["max_output_tokens_per_round"])
        self.assertGreater(result.enabled.compaction_count, 0)
        self.assertEqual(result.baseline.compaction_count, 0)

    def test_all_variants_preserve_declared_correctness_guardrails(self) -> None:
        """四组 ON/OFF 对照均应通过既有 Runtime correctness 边界。"""

        report = run_benchmark()

        self.assertEqual(len(report.results), 4)
        self.assertEqual(report.passed, 4)
        for result in report.results:
            with self.subTest(scenario=result.scenario):
                self.assertEqual(
                    result.baseline.evidence_type,
                    "DETERMINISTIC_MECHANISM_EVIDENCE",
                )
                self.assertEqual(
                    result.enabled.evidence_type,
                    "DETERMINISTIC_MECHANISM_EVIDENCE",
                )
                self.assertTrue(all(result.baseline.correctness_guardrails.values()))
                self.assertTrue(all(result.enabled.correctness_guardrails.values()))

    def test_paired_metrics_do_not_overclaim_cache_or_retrieval_benefits(self) -> None:
        """缓存只减少正文注入，Record/Memory 只报告注入开销。"""

        results = {item.scenario: item for item in run_benchmark().results}
        compression = results["C1_context_compression"]
        reuse = results["C2_read_file_content_reuse"]
        context_record = results["C3_context_record_retrieval"]
        memory = results["C4_workspace_memory_retrieval"]

        self.assertGreater(compression.enabled.compaction_count, 0)
        self.assertEqual(compression.baseline.compaction_count, 0)
        self.assertEqual(reuse.enabled.tool_calls, reuse.baseline.tool_calls)
        self.assertEqual(reuse.enabled.mechanism_specific_metrics["tool_dispatch_saved"], 0)
        self.assertIsNone(reuse.enabled.mechanism_specific_metrics["physical_file_io_saved"])
        self.assertFalse(context_record.enabled.mechanism_specific_metrics["automatic_behavioral_savings_claimed"])
        self.assertFalse(memory.enabled.mechanism_specific_metrics["automatic_behavioral_savings_claimed"])
        self.assertFalse(memory.enabled.mechanism_specific_metrics["auto_extraction_evaluated"])


if __name__ == "__main__":
    unittest.main()
