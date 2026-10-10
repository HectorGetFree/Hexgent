"""Phase5C-2 runner 的本地分类与 L1 专用 oracle 契约。"""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from eval.phase5c.fixtures import workspace_manifest
from eval.phase5c.observation import RuntimeObservation, ToolCallDiagnostic
from eval.phase5c.oracles import OracleResult
from eval.phase5c.smoke import _attribute_failure, _l1_stage_a_oracle, _qualification, _shortcut_flags
from eval.phase5c.specifications import task_by_id


class Phase5CSmokeRunnerTest(unittest.TestCase):
    def test_provider_timeout_and_stream_interruption_are_not_task_failures(self) -> None:
        observation = RuntimeObservation(
            None,
            None,
            ({"reason": "stream_error", "message": "模型流式响应错误：Request timed out."},),
            (),
        )
        self.assertEqual(
            _attribute_failure({}, observation, OracleResult("ORACLE_FAIL", {}, ()), None),
            "PROVIDER_TIMEOUT",
        )
        interrupted = RuntimeObservation(
            None,
            None,
            ({"reason": "stream_error", "message": "peer closed connection without sending complete message body"},),
            (),
        )
        self.assertEqual(
            _attribute_failure({}, interrupted, OracleResult("ORACLE_FAIL", {}, ()), None),
            "PROVIDER_STREAM_INTERRUPTED",
        )

    def test_provider_policy_and_response_binding_failures_are_not_model_errors(self) -> None:
        policy_blocked = RuntimeObservation(
            None,
            None,
            ({"reason": "response_failed", "message": "Request blocked by upstream policy."},),
            (),
        )
        self.assertEqual(
            _attribute_failure({}, policy_blocked, OracleResult("ORACLE_FAIL", {}, ()), None),
            "PROVIDER_UPSTREAM_POLICY_BLOCK",
        )
        binding_changed = RuntimeObservation(
            None,
            None,
            ({"reason": "stream_error", "message": "previous response account binding is unavailable or changed"},),
            (),
        )
        self.assertEqual(
            _attribute_failure({}, binding_changed, OracleResult("ORACLE_FAIL", {}, ()), None),
            "PROVIDER_RESPONSE_BINDING_ERROR",
        )

    def test_provider_overload_and_account_state_are_not_model_errors(self) -> None:
        overloaded = RuntimeObservation(
            None, None, ({"reason": "stream_error", "message": "Provider currently overloaded (503)"},), (),
        )
        self.assertEqual(
            _attribute_failure({}, overloaded, OracleResult("ORACLE_FAIL", {}, ()), None),
            "PROVIDER_OVERLOADED",
        )
        account_disabled = RuntimeObservation(
            None, None, ({"reason": "response_failed", "message": "account is no longer enabled for this key"},), (),
        )
        self.assertEqual(
            _attribute_failure({}, account_disabled, OracleResult("ORACLE_FAIL", {}, ()), None),
            "PROVIDER_ACCOUNT_DISABLED",
        )

    def test_l1_stage_a_uses_durable_state_oracle_not_file_artifact_oracle(self) -> None:
        with TemporaryDirectory() as directory:
            workspace = Path(directory)
            before = workspace_manifest(workspace)
            oracle = _l1_stage_a_oracle(
                workspace,
                before,
                {"status": "completed"},
                [{"type": "context_record_extraction_started"}],
            )
            self.assertEqual(oracle.state, "ORACLE_PASS")
            self.assertTrue(oracle.checks["extraction_path_executed"])

    def test_explicit_output_name_is_not_treated_as_fixture_information_leak(self) -> None:
        spec = task_by_id("W1")
        flags = _shortcut_flags(
            spec,
            RuntimeObservation("completed", None, (), ()),
            OracleResult("ORACLE_FAIL", {}, ()),
        )
        self.assertNotIn("FIXTURE_INFORMATION_LEAK: prompt_names_input_source", flags)

    def test_l1_read_only_stage_does_not_require_workspace_mutation(self) -> None:
        spec = task_by_id("L1")
        flags = _shortcut_flags(
            spec,
            RuntimeObservation("completed", None, (), ()),
            OracleResult("ORACLE_PASS", {}, (), ""),
        )
        self.assertNotIn("ORACLE_DESIGN_FAILURE: pass_without_workspace_change", flags)

    def test_oracle_pass_with_intermediate_tool_failure_is_qualified(self) -> None:
        spec = task_by_id("T1")
        observation = RuntimeObservation(
            "completed",
            None,
            (),
            (ToolCallDiagnostic("call-1", "bash", "error", "business_failure", "allow", True),),
        )
        qualification, _ = _qualification(
            spec,
            observation,
            OracleResult("ORACLE_PASS", {}, ("app/parser.py",), ""),
            "TOOL_FAILURE",
            (),
        )
        self.assertEqual("QUALIFIED_FOR_FINAL_BENCHMARK", qualification)
