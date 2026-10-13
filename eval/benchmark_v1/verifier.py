"""确定性只读 Verifier 骨架：只核对输入快照与证据文件，不连接业务系统。"""

from hashlib import sha256
import json
from pathlib import Path
from typing import Protocol

from pydantic import JsonValue, ValidationError

from .contracts import (
    ArtifactReference, AssertionResult, ContractModel, Identifier, TaskContract,
    TrialManifest, VerifierResult,
)


class Observation(ContractModel):
    """由独立观察器提供；field → evidence 显式绑定，不接受 Agent 自评。"""

    values: dict[str, JsonValue]
    evidence: dict[str, tuple[ArtifactReference, ...]]


class VerificationInput(ContractModel):
    contract: TaskContract
    trial: TrialManifest
    artifact_root: Path
    observation_suite_id: Identifier
    observation_case_id: Identifier
    observation_trial_id: Identifier
    # 未知就绪状态使用 None；不能把未知环境默认为有效。
    environment_valid: bool | None
    environment_evidence: tuple[ArtifactReference, ...]
    business_state: Observation
    tool_audit: Observation


class DeterministicVerifier(Protocol):
    def verify(self, inputs: VerificationInput) -> VerifierResult:
        """只读验证；不调用 LLM，不写业务状态。"""
        ...


def _evidence_available(root: Path, references: tuple[ArtifactReference, ...]) -> bool:
    if not references:
        return False
    for reference in references:
        target = (root / reference.path).resolve()
        if not target.is_relative_to(root.resolve()):
            return False
        try:
            if not target.is_file():
                return False
            if reference.sha256 and sha256(target.read_bytes()).hexdigest() != reference.sha256:
                return False
        except OSError:
            return False
    return True


def _equal(left: JsonValue, right: JsonValue) -> bool:
    # JSON 编码避免 Python 将 True==1 误判为相等；对象键顺序不影响结果。
    options = dict(sort_keys=True, ensure_ascii=False, allow_nan=False)
    return json.dumps(left, **options) == json.dumps(right, **options)


class FieldEqualityVerifier:
    """固定顶层字段相等断言，足够验收骨架；不是任务 DSL。"""

    def verify(self, inputs: VerificationInput) -> VerifierResult:
        contract, trial = inputs.contract, inputs.trial
        assertions = []

        def result(status, category):
            return VerifierResult(schema_version=1, suite_id=trial.suite_id,
                                  case_id=trial.case_id, trial_id=trial.trial_id,
                                  status=status, assertions=tuple(assertions),
                                  failure_category=category, verifier_version="1")

        # 1. 合同或独立观察结果串 Trial，属于实验协议失效，不给 Agent 记失败。
        if not trial.matches(contract) or (
                inputs.observation_suite_id, inputs.observation_case_id,
                inputs.observation_trial_id) != (trial.suite_id, trial.case_id, trial.trial_id):
            assertions.append(AssertionResult(assertion_id="identity", category="protocol",
                                              status="FAIL", evidence=(), detail="合同或观察结果标识不匹配"))
            return result("INVALID", "protocol")

        # 冻结文件与传入合同都要核对，不能仅靠内存中的相同 ID。
        contract_available = _evidence_available(inputs.artifact_root, (trial.contract,))
        if contract_available:
            try:
                saved = TaskContract.model_validate_json(
                    (inputs.artifact_root / trial.contract.path).read_text(encoding="utf-8"))
                if saved.digest() != contract.digest():
                    assertions.append(AssertionResult(assertion_id="contract_artifact", category="protocol",
                        status="FAIL", evidence=(trial.contract,), detail="冻结合同文件与输入合同不同"))
                    return result("INVALID", "protocol")
            except (OSError, ValueError, ValidationError):
                assertions.append(AssertionResult(assertion_id="contract_artifact", category="protocol",
                    status="FAIL", evidence=(trial.contract,), detail="冻结合同文件不可解析"))
                return result("INVALID", "protocol")
        assertions.append(AssertionResult(assertion_id="contract_artifact", category="protocol",
                                          status="PASS" if contract_available else "MISSING",
                                          evidence=(trial.contract,), detail="冻结合同文件一致性"))
        session_references = tuple(reference for session in trial.sessions
                                   for reference in (session.transcript, *session.run_reports))
        assertions.append(AssertionResult(assertion_id="session_artifacts", category="protocol",
                                          status="PASS" if _evidence_available(
                                              inputs.artifact_root, session_references) else "MISSING",
                                          evidence=session_references, detail="Session 审计文件可读取"))

        # 2. 显式环境失效与未知分开；缺少环境证据不得进入 PASS。
        available = _evidence_available(inputs.artifact_root, inputs.environment_evidence)
        environment_status = ("MISSING" if inputs.environment_valid is None or not available
                              else "PASS" if inputs.environment_valid else "FAIL")
        assertions.append(AssertionResult(assertion_id="environment", category="protocol",
                                          status=environment_status,
                                          evidence=inputs.environment_evidence,
                                          detail="独立环境就绪观测"))
        if environment_status == "FAIL":
            return result("INVALID", "environment")

        # 3. 断言只使用独立快照和审计，不读取最终回答，也不限制工具顺序。
        for spec in contract.verifier_spec.assertions:
            observation = getattr(inputs, spec.source)
            references = observation.evidence.get(spec.field, ())
            if spec.field not in observation.values or not _evidence_available(
                    inputs.artifact_root, references):
                status, detail = "MISSING", "字段或可读取证据缺失／摘要不匹配"
            else:
                status = ("PASS" if _equal(observation.values[spec.field],
                                          contract.expected_outcome[spec.expected_key]) else "FAIL")
                detail = "类型敏感的字段相等比较"
            assertions.append(AssertionResult(assertion_id=spec.assertion_id,
                                              category=spec.category, status=status,
                                              evidence=references, detail=detail))

        # 4. 协议失效优先；明确业务失败不能被 Trace 缺失掩盖。
        if any(a.category == "protocol" and a.status == "FAIL" for a in assertions):
            return result("INVALID", "protocol")
        for category in ("safety", "outcome"):
            if any(a.category == category and a.status == "FAIL" for a in assertions):
                return result("FAIL", category)
        if any(a.status == "MISSING" for a in assertions):
            return result("INCONCLUSIVE", "missing_evidence")
        if any(a.category == "trace" and a.status == "FAIL" for a in assertions):
            return result("INCONCLUSIVE", "attribution")
        return result("PASS", None)
