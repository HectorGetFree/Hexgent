"""纯离线合同验收；不导入 Hexgent Runtime，也不访问网络或业务服务。"""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from .artifacts import read_json, write_json
from .contracts import (
    ArtifactReference, SessionArtifacts, TaskContract, TrialManifest, VerifierResult,
)
from .verifier import FieldEqualityVerifier, Observation, VerificationInput


EXAMPLE = Path(__file__).with_name("examples") / "local_contract.json"


def contract_data():
    return json.loads(EXAMPLE.read_text(encoding="utf-8"))


def parse(data):
    return TaskContract.model_validate_json(json.dumps(data))


class ContractTests(unittest.TestCase):
    def test_valid_contract(self):
        self.assertEqual(parse(contract_data()).schema_version, 1)

    def test_every_required_field(self):
        for key in contract_data():
            with self.subTest(key=key):
                data = contract_data(); del data[key]
                with self.assertRaises(ValidationError): parse(data)

    def test_enums_and_unknown_fields(self):
        for key, value in [("environment_kind", "mock"), ("extra", "ignored")]:
            data = contract_data(); data[key] = value
            with self.assertRaises(ValidationError): parse(data)

    def test_illegal_budgets(self):
        for field in contract_data()["execution_budget"]:
            for value in [0, -1, True, "10", float("inf"), float("nan")]:
                with self.subTest(field=field, value=value):
                    data = contract_data(); data["execution_budget"][field] = value
                    with self.assertRaises((ValidationError, ValueError)): parse(data)
        data = contract_data(); data["execution_budget"]["max_tool_calls"] = 1.5
        with self.assertRaises(ValidationError): parse(data)

    def test_unsupported_versions(self):
        for value in [0, 2, "1", True, 1.0]:
            data = contract_data(); data["schema_version"] = value
            with self.assertRaises(ValidationError): parse(data)

    def test_visible_projection(self):
        contract = parse(contract_data()); visible = contract.agent_visible().model_dump(mode="json")
        self.assertEqual(set(visible), {"user_query", "allowed_tools", "execution_budget"})
        for secret in ["hidden_fault_plan", "expected_outcome", "verifier_spec", "protected_changed"]:
            self.assertNotIn(secret, json.dumps(visible))

    def test_bad_assertion_contracts(self):
        for change in ["duplicate", "reserved", "no_safety", "missing_key", "audit_outcome"]:
            data = contract_data(); checks = data["verifier_spec"]["assertions"]
            if change == "duplicate": checks[1]["assertion_id"] = checks[0]["assertion_id"]
            elif change == "reserved": checks[0]["assertion_id"] = "environment"
            elif change == "no_safety": checks[1]["category"] = "trace"
            elif change == "missing_key": checks[0]["expected_key"] = "unknown"
            else: checks[0]["source"] = "tool_audit"
            with self.assertRaises(ValidationError): parse(data)

    def test_path_escape(self):
        for path in ["../state.json", "/tmp/state.json", "a/../state", "C:/state", "a\\state", "./state", "."]:
            with self.assertRaises(ValidationError): ArtifactReference(path=path)


class VerifierTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.contract = parse(contract_data())
        write_json(self.root / "contract.json", self.contract)
        for name in ["state.json", "environment.json", "sessions/s1/transcript.jsonl",
                     "sessions/s1/reports/run-001.json", "sessions/s2/transcript.jsonl"]:
            path = self.root / name; path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{}\n', encoding="utf-8")
        self.trial = TrialManifest(
            schema_version=1, suite_id=self.contract.suite_id, task_id=self.contract.task_id,
            case_id=self.contract.case_id, trial_id="trial-001", environment_kind="business_fixture",
            phase="protocol_smoke", baseline_id=self.contract.baseline_id, verifier_version="1",
            task_contract_sha256=self.contract.digest(), contract=ArtifactReference(path="contract.json"),
            run_config={"variant": "full", "provider_execution": False}, code_versions={"hexgent": "offline-fixture"},
            sessions=(SessionArtifacts(session_id="s1", transcript=ArtifactReference(path="sessions/s1/transcript.jsonl"),
                                       run_reports=(ArtifactReference(path="sessions/s1/reports/run-001.json"),), provider_usage=()),
                      SessionArtifacts(session_id="s2", transcript=ArtifactReference(path="sessions/s2/transcript.jsonl"),
                                       run_reports=(), provider_usage=())))
        self.state = Observation(values={"status": "done", "protected_changed": False},
                                 evidence={key: (ArtifactReference(path="state.json"),)
                                           for key in ["status", "protected_changed"]})

    def inputs(self, **changes):
        values = dict(contract=self.contract, trial=self.trial, artifact_root=self.root,
                      observation_suite_id=self.trial.suite_id, observation_case_id=self.trial.case_id,
                      observation_trial_id=self.trial.trial_id, environment_valid=True,
                      environment_evidence=(ArtifactReference(path="environment.json"),),
                      business_state=self.state, tool_audit=Observation(values={}, evidence={}))
        values.update(changes)
        return VerificationInput(**values)

    def verify(self, **changes):
        return FieldEqualityVerifier().verify(self.inputs(**changes))

    def test_pass(self):
        self.assertEqual(self.verify().status, "PASS")

    def test_outcome_fail(self):
        self.assertEqual(self.verify(business_state=Observation(
            values={"status": "pending"}, evidence=self.state.evidence)).status, "FAIL")

    def test_safety_fail(self):
        state = Observation(values={"status": "done", "protected_changed": True}, evidence=self.state.evidence)
        result = self.verify(business_state=state)
        self.assertEqual((result.status, result.failure_category), ("FAIL", "safety"))

    def test_environment_invalid(self):
        self.assertEqual(self.verify(environment_valid=False).status, "INVALID")

    def test_environment_unknown(self):
        self.assertEqual(self.verify(environment_valid=None).status, "INCONCLUSIVE")

    def test_business_evidence_missing(self):
        for state in [Observation(values={}, evidence={}),
                      Observation(values=self.state.values, evidence={})]:
            self.assertEqual(self.verify(business_state=state).status, "INCONCLUSIVE")

    def test_evidence_file_missing_and_digest_wrong(self):
        (self.root / "state.json").unlink()
        self.assertEqual(self.verify().status, "INCONCLUSIVE")
        (self.root / "state.json").write_text('{}\n')
        refs = {key: (ArtifactReference(path="state.json", sha256="0" * 64),) for key in self.state.values}
        self.assertEqual(self.verify(business_state=Observation(values=self.state.values, evidence=refs)).status,
                         "INCONCLUSIVE")

    def test_observation_identity_mismatch(self):
        for key in ["observation_suite_id", "observation_case_id", "observation_trial_id"]:
            self.assertEqual(self.verify(**{key: "other"}).status, "INVALID")

    def test_trial_contract_mismatch(self):
        for key in ["suite_id", "task_id", "case_id", "baseline_id", "task_contract_sha256"]:
            data = self.trial.model_dump(mode="json"); data[key] = "0" * 64 if key.endswith("sha256") else "other"
            trial = TrialManifest.model_validate_json(json.dumps(data))
            self.assertEqual(self.verify(trial=trial).status, "INVALID")

    def test_contract_mutation_detected(self):
        self.contract.expected_outcome["final_status"] = "changed"
        self.assertEqual(self.verify().status, "INVALID")

    def test_frozen_contract_file_mismatch(self):
        data = contract_data(); data["user_query"] = "changed"
        write_json(self.root / "contract.json", parse(data))
        self.assertEqual(self.verify().status, "INVALID")

    def test_missing_contract_and_transcript(self):
        for path in ["contract.json", "sessions/s2/transcript.jsonl"]:
            raw = (self.root / path).read_bytes(); (self.root / path).unlink()
            self.assertEqual(self.verify().status, "INCONCLUSIVE")
            (self.root / path).write_bytes(raw)

    def test_trace_not_business_success(self):
        data = contract_data(); data["expected_outcome"]["compacted"] = True
        data["verifier_spec"]["assertions"].append(dict(assertion_id="trace", category="trace",
            source="tool_audit", field="compacted", expected_key="compacted"))
        self.contract = parse(data)
        self.trial = self.trial.model_copy(update={"task_contract_sha256": self.contract.digest()})
        write_json(self.root / "contract.json", self.contract)
        audit = Observation(values={"compacted": False}, evidence={"compacted": (ArtifactReference(path="state.json"),)})
        self.assertEqual(self.verify(tool_audit=audit).failure_category, "attribution")
        state = Observation(values={"status": "failed", "protected_changed": False}, evidence=self.state.evidence)
        self.assertEqual(self.verify(tool_audit=audit, business_state=state).status, "FAIL")

    def test_protocol_assertion_invalid(self):
        data = contract_data(); data["expected_outcome"]["seed_valid"] = True
        data["verifier_spec"]["assertions"].append(dict(assertion_id="seed", category="protocol",
            source="business_state", field="seed_valid", expected_key="seed_valid"))
        self.contract = parse(data)
        self.trial = self.trial.model_copy(update={"task_contract_sha256": self.contract.digest()})
        write_json(self.root / "contract.json", self.contract)
        state = Observation(values={**self.state.values, "seed_valid": False},
                            evidence={**self.state.evidence, "seed_valid": (ArtifactReference(path="state.json"),)})
        result = self.verify(business_state=state)
        self.assertEqual((result.status, result.failure_category), ("INVALID", "protocol"))

    def test_symlink_evidence_escape(self):
        with TemporaryDirectory() as external:
            source = Path(external) / "state.json"; source.write_text('{}')
            (self.root / "state.json").unlink(); (self.root / "state.json").symlink_to(source)
            self.assertEqual(self.verify().status, "INCONCLUSIVE")

    def test_serialized_version_and_result_status_validation(self):
        for model, data in [(TrialManifest, self.trial.model_dump(mode="json")),
                            (VerifierResult, self.verify().model_dump(mode="json"))]:
            data["schema_version"] = 2
            with self.assertRaises(ValidationError): model.model_validate_json(json.dumps(data))
        data = self.verify().model_dump(mode="json")
        data.update(status="INVALID", failure_category="outcome")
        with self.assertRaises(ValidationError): VerifierResult.model_validate_json(json.dumps(data))

    def test_type_sensitive_equality(self):
        state = Observation(values={"status": "done", "protected_changed": 0}, evidence=self.state.evidence)
        self.assertEqual(self.verify(business_state=state).status, "FAIL")

    def test_multiple_sessions_and_roundtrip(self):
        result = self.verify()
        artifact = self.trial.model_copy(update={"verifier_result": result})
        write_json(self.root / "trial.json", artifact)
        restored = read_json(self.root / "trial.json", TrialManifest)
        self.assertEqual(restored, artifact)
        self.assertEqual([s.session_id for s in restored.sessions], ["s1", "s2"])
        self.assertEqual(read_json(self.root / "contract.json", TaskContract), self.contract)
        write_json(self.root / "result.json", result)
        self.assertEqual(read_json(self.root / "result.json", VerifierResult), result)

    def test_session_and_result_identity_mixup(self):
        data = self.trial.model_dump(mode="json")
        data["sessions"][1]["session_id"] = "s1"
        with self.assertRaises(ValidationError): TrialManifest.model_validate_json(json.dumps(data))
        data = self.trial.model_dump(mode="json"); data["verifier_result"] = self.verify().model_dump(mode="json")
        data["verifier_result"]["trial_id"] = "other"
        with self.assertRaises(ValidationError): TrialManifest.model_validate_json(json.dumps(data))

    def test_verifier_readonly_and_no_network(self):
        before = {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        with patch('socket.socket', side_effect=AssertionError("禁止网络")), \
                patch.object(Path, 'write_text', side_effect=AssertionError("禁止写入")), \
                patch.object(Path, 'write_bytes', side_effect=AssertionError("禁止写入")):
            self.assertEqual(self.verify().status, "PASS")
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_full_ablation_share_contract(self):
        variant = self.trial.model_copy(update={"phase": "ablation", "trial_id": "trial-002",
                                                "run_config": {"variant": "no_memory"}})
        self.assertTrue(variant.matches(self.contract))
        self.assertEqual(variant.task_contract_sha256, self.trial.task_contract_sha256)

    def test_assertion_order_independent(self):
        data = contract_data(); data["verifier_spec"]["assertions"].reverse()
        self.contract = parse(data)
        self.trial = self.trial.model_copy(update={"task_contract_sha256": self.contract.digest()})
        write_json(self.root / "contract.json", self.contract)
        self.assertEqual(self.verify().status, "PASS")


if __name__ == "__main__":
    unittest.main()
