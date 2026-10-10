"""Batch 1 聚合只测试统计语义，不发起真实模型调用。"""

import unittest

from .batch1 import _l1_summary, _l1_stage_b_eligibility, _summarize_task, _usage_metrics


class Batch1MetricsTest(unittest.TestCase):
    def test_partial_usage_is_not_complete_cost(self) -> None:
        metrics = _usage_metrics([
            {"completed": True, "input_tokens": 10, "output_tokens": 2},
            {"completed": False, "input_tokens": None, "output_tokens": None},
        ])
        self.assertEqual(metrics["usage_completeness"], "PARTIAL")
        self.assertEqual(metrics["usage_coverage_ratio"], 0.5)
        self.assertEqual(metrics["observed_total_tokens"], 12)

    def test_no_usage_is_unavailable(self) -> None:
        metrics = _usage_metrics([{"completed": False, "input_tokens": None, "output_tokens": None}])
        self.assertEqual(metrics["usage_completeness"], "UNAVAILABLE")
        self.assertIsNone(metrics["observed_total_tokens"])

    def test_l1_does_not_start_stage_b_when_terminal_failed_even_if_record_qualifies(self) -> None:
        eligible, reason = _l1_stage_b_eligibility(
            agent_completed=False,
            extraction_path_executed=True,
            record_qualified=True,
            record_reason="QUALIFIED",
        )

        self.assertFalse(eligible)
        self.assertEqual(reason, "STAGE_A_NOT_COMPLETED")

    def test_l1_requires_extraction_and_qualified_record_before_stage_b(self) -> None:
        eligible, reason = _l1_stage_b_eligibility(
            agent_completed=True,
            extraction_path_executed=False,
            record_qualified=True,
            record_reason="QUALIFIED",
        )

        self.assertFalse(eligible)
        self.assertEqual(reason, "STAGE_A_EXTRACTION_NOT_EXECUTED")

    def test_summary_separates_terminal_oracle_and_end_to_end_outcomes(self) -> None:
        def row(*, completed: bool, oracle: str, attribution: str | None = None) -> dict:
            return {
                "agent_completed": completed,
                "oracle": {"state": oracle},
                "metadata": {
                    "failure_attribution": attribution,
                    "usage": {"usage_eligible_calls": 0, "usage_covered_calls": 0, "usage_completeness": "UNAVAILABLE"},
                    "model_rounds": 0,
                    "latency_ms": 0,
                    "runtime_metrics": {
                        "unique_files_read": 0,
                        "repeated_file_reads": 0,
                        "compaction_count": 0,
                    },
                },
                "runtime": {"total_tool_calls": 0, "tool_calls": []},
            }

        summary = _summarize_task([
            row(completed=True, oracle="ORACLE_PASS"),
            row(completed=False, oracle="ORACLE_PASS", attribution="MODEL_RESPONSE_ERROR"),
            row(completed=True, oracle="ORACLE_FAIL"),
        ])

        self.assertEqual(summary["terminal_completed_runs"], 2)
        self.assertEqual(summary["oracle_pass_runs"], 2)
        self.assertEqual(summary["end_to_end_success_runs"], 1)
        self.assertEqual(summary["end_to_end_success_rate"], 1 / 3)

    def test_l1_summary_does_not_count_failed_stage_a_or_no_retrieval_pair_as_feasible(self) -> None:
        stage_a = {
            "agent_completed": False,
            "oracle": {"checks": {"extraction_path_executed": True}},
            "metadata": {
                "l1_qualification": {
                    "active_record_count": 1,
                    "record_qualified": True,
                    "stage_b_eligible": False,
                    "stage_b_eligibility_reason": "STAGE_A_NOT_COMPLETED",
                },
            },
        }
        summary = _l1_summary([{
            "stage_a": stage_a,
            "stage_b": None,
            "stage_b_pair": {"state": "NOT_ELIGIBLE", "reason": "STAGE_A_NOT_COMPLETED"},
        }])

        self.assertEqual(summary["record_qualified_count"], 1)
        self.assertEqual(summary["qualified_record_observed_count"], 1)
        self.assertEqual(summary["stage_b_pair_eligible_count"], 0)
        self.assertEqual(summary["qualified_stage_a_count"], 0)
        self.assertEqual(summary["stage_b_pair_executed_count"], 0)
        self.assertEqual(summary["stage_b_pair_with_retrieval_count"], 0)
        self.assertEqual(summary["stage_b_ineligibility_reasons"], {"STAGE_A_NOT_COMPLETED": 1})

    def test_provider_policy_and_binding_errors_count_as_infrastructure_failures(self) -> None:
        def row(attribution: str) -> dict:
            return {
                "agent_completed": False,
                "oracle": {"state": "ORACLE_FAIL"},
                "metadata": {
                    "failure_attribution": attribution,
                    "usage": {"usage_eligible_calls": 0, "usage_covered_calls": 0, "usage_completeness": "UNAVAILABLE"},
                    "model_rounds": 0,
                    "latency_ms": 0,
                    "runtime_metrics": {
                        "unique_files_read": 0,
                        "repeated_file_reads": 0,
                        "compaction_count": 0,
                    },
                },
                "runtime": {"total_tool_calls": 0, "tool_calls": []},
            }

        summary = _summarize_task([
            row("PROVIDER_UPSTREAM_POLICY_BLOCK"),
            row("PROVIDER_RESPONSE_BINDING_ERROR"),
            row("PROVIDER_OVERLOADED"),
            row("PROVIDER_ACCOUNT_DISABLED"),
        ])

        self.assertEqual(summary["infrastructure_failures"], 4)
