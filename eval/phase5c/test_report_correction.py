"""已有 Artifact 的离线修正合同，不调用 Provider。"""

import unittest
from copy import deepcopy

from .batch1 import _mean_median, _usage_metrics
from .correct_batch1 import correct_run, denial_audit
from .observation import RuntimeObservation, ToolCallDiagnostic
from .oracles import OracleResult
from .provider_preflight import classify_preflight_failure
from .smoke import _qualification
from .specifications import task_by_id


class ReportCorrectionTest(unittest.TestCase):
    def test_correction_preserves_source_evidence_and_marks_provider_run_not_evaluable(self):
        source = {
            "agent_completed": False,
            "oracle": {"state": "ORACLE_FAIL"},
            "metadata": {"failure_attribution": "MODEL_RESPONSE_ERROR", "provider_calls": [], "usage": {}},
            "runtime": {"model_response_failed": [{"reason": "stream_error", "message": "upstream request failed for model gpt-6.1-sol"}],
                        "tool_calls": [], "denied_calls": 0, "failed_calls": 0},
        }
        before = deepcopy(source)
        corrected = correct_run(source)
        self.assertEqual(source, before)
        self.assertEqual(corrected["metadata"]["failure_attribution"], "PROVIDER_UPSTREAM_REQUEST_FAILED")
        self.assertIsNone(corrected["corrected_assessment"]["task_qualified"])
        self.assertFalse(corrected["corrected_assessment"]["agent_task_failure"])
        self.assertEqual(corrected["runtime"]["model_response_failed"], source["runtime"]["model_response_failed"])

    def test_missing_command_does_not_invent_denial_category_or_repeat(self):
        audit = denial_audit([{
            "metadata": {"batch_run_index": 1},
            "runtime": {"tool_calls": [{"call_id": "call", "tool_name": "bash", "status": "denied",
                "error_category": "eval_tool_policy_denied", "permission_outcome": "allow", "dispatch_occurred": True}]},
        }])
        self.assertEqual(audit[0]["category"], "UNKNOWN")
        self.assertEqual(audit[0]["command_observability"], "COMMAND_NOT_OBSERVABLE")
        self.assertIsNone(audit[0]["repeated_attempt"])

    def test_upstream_failure_does_not_invent_specific_cause(self):
        self.assertEqual(
            classify_preflight_failure("upstream request failed for model gpt-6.1-sol"),
            "PROVIDER_UPSTREAM_REQUEST_FAILED",
        )

    def test_complete_usage_aggregates_nested_provider_values(self):
        usage = _usage_metrics([{"completed": True, "input_tokens": 123, "output_tokens": 45}])
        result = _mean_median([{"metadata": {"usage": usage}}], "observed_input_tokens", complete_only=True)
        self.assertEqual(result, {"mean": 123, "median": 123, "sample_count": 1})

    def test_missing_extraction_usage_is_partial_not_estimated_cost(self):
        usage = _usage_metrics([
            {"completed": True, "input_tokens": 123, "output_tokens": 45},
            {"completed": False, "input_tokens": None, "output_tokens": None, "estimated_input_tokens": 999},
        ])
        self.assertEqual(usage["usage_completeness"], "PARTIAL")
        self.assertEqual(usage["observed_input_tokens"], 123)
        self.assertEqual(usage["usage_eligible_calls"], 2)

    def test_recovered_policy_denial_does_not_disqualify_successful_task(self):
        observation = RuntimeObservation("completed", None, (), (
            ToolCallDiagnostic("c", "bash", "denied", "eval_tool_policy_denied", "allow", True),
        ))
        qualification, _ = _qualification(
            task_by_id("T1"), observation,
            OracleResult("ORACLE_PASS", {"tests_pass": True, "only_parser_changed": True}, ("app/parser.py",)),
            "PERMISSION_FAILURE", (),
        )
        self.assertEqual(qualification, "QUALIFIED_FOR_FINAL_BENCHMARK")
