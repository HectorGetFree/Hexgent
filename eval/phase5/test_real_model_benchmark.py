"""Phase5B 结构测试；真实模型调用只允许通过 real_runner 显式执行。"""

from __future__ import annotations

import unittest

from eval.phase5.real_model import IndividualRun, VariantAggregate, _delta


def _run(*, input_tokens: int | None, output_tokens: int | None, success: bool = True) -> IndividualRun:
    return IndividualRun(
        scenario="T", variant="V", index=1, success=success, terminal_status="completed",
        final_output="ok", provider_input_tokens=input_tokens, provider_output_tokens=output_tokens,
        estimated_input_tokens=100, model_rounds=1, tool_calls=1, read_file_calls=1,
        unique_files_read=1, repeated_exploration=0, compaction_count=0, injected_tokens=0,
        latency_ms=10.0, guardrails={"ok": True}, provider_usage_complete=input_tokens is not None,
    )


class RealModelBenchmarkStructureTest(unittest.TestCase):
    def test_aggregate_preserves_raw_runs_and_marks_small_sample_p95(self) -> None:
        aggregate = VariantAggregate("T", "on", (_run(input_tokens=10, output_tokens=3), _run(input_tokens=20, output_tokens=5)))
        report = aggregate.to_dict()
        self.assertEqual(report["success"]["count"], 2)
        self.assertEqual(len(report["individual_runs"]), 2)
        self.assertEqual(report["latency_ms"]["p95_small_sample_diagnostic"], 10.0)

    def test_delta_does_not_fabricate_missing_provider_usage(self) -> None:
        baseline = VariantAggregate("T", "off", (_run(input_tokens=None, output_tokens=None),)).to_dict()
        enabled = VariantAggregate("T", "on", (_run(input_tokens=10, output_tokens=3),)).to_dict()
        self.assertIsNone(_delta(baseline, enabled)["provider_input_tokens"]["mean_delta"])
