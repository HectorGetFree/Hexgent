"""B1-A 任务与 Trial 数据合同，复用项目现有 Pydantic / JSON 方式。"""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import PurePosixPath
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator


Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")]
Nonempty = Annotated[str, Field(min_length=1)]
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class ContractModel(BaseModel):
    """拒绝未知字段和类型强转；冻结属性，嵌套 JSON 用摘要检测变化。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)

    @model_validator(mode="after")
    def finite_json(self):
        json.dumps(self.model_dump(mode="json"), allow_nan=False)
        return self


class VersionedModel(ContractModel):
    schema_version: Literal[1]

    @field_validator("schema_version", mode="before")
    @classmethod
    def supported_version(cls, value):
        if type(value) is not int or value != 1:
            raise ValueError("仅支持整数 schema_version=1")
        return value


class ExecutionBudget(ContractModel):
    # 累计所有 Provider 请求的输入与输出 Token，包括提取和压缩请求。
    max_provider_tokens: Annotated[int, Field(gt=0)]
    # 真实派发尝试的总数；工具重试计入预算，不产生新 Trial。
    max_tool_calls: Annotated[int, Field(gt=0)]
    # 整个 Trial 的墙钟秒数，包含多 Session；本轮只定义，不执行限额。
    max_wall_time_seconds: Annotated[float, Field(gt=0)]


class AssertionSpec(ContractModel):
    assertion_id: Identifier
    category: Literal["outcome", "safety", "protocol", "trace"]
    source: Literal["business_state", "tool_audit"]
    field: Nonempty
    # 只查一个顶层字段并作 JSON 类型敏感相等比较，不支持表达式或点路径。
    expected_key: Nonempty


class VerifierSpec(ContractModel):
    kind: Literal["field_equality_v1"]
    verifier_version: Literal["1"]
    assertions: tuple[AssertionSpec, ...] = Field(min_length=2)

    @model_validator(mode="after")
    def validate_assertions(self):
        ids = [item.assertion_id for item in self.assertions]
        if len(ids) != len(set(ids)):
            raise ValueError("断言 ID 不能重复")
        if set(ids) & {"identity", "environment", "contract_artifact", "session_artifacts"}:
            raise ValueError("断言 ID 与 Verifier 内置协议检查冲突")
        if not {"outcome", "safety"} <= {item.category for item in self.assertions}:
            raise ValueError("至少需要一项 Outcome 和一项 Safety 断言")
        if any(item.category == "outcome" and item.source != "business_state"
               for item in self.assertions):
            raise ValueError("Outcome 必须使用独立业务状态证据")
        return self


class AgentVisibleTask(ContractModel):
    """唯一可交给 Agent 的投影；不携带场景内部参数或评分规则。"""

    user_query: Nonempty
    allowed_tools: tuple[Identifier, ...]
    execution_budget: ExecutionBudget


class TaskContract(VersionedModel):
    suite_id: Identifier
    task_id: Identifier
    case_id: Identifier
    environment_kind: Literal["hexworkspace", "business_fixture"]
    baseline_id: Identifier
    user_query: Nonempty
    allowed_tools: tuple[Identifier, ...]
    execution_budget: ExecutionBudget
    scenario_config: dict[str, JsonValue]
    expected_outcome: dict[str, JsonValue]
    verifier_spec: VerifierSpec

    @model_validator(mode="after")
    def validate_task(self):
        if len(self.allowed_tools) != len(set(self.allowed_tools)):
            raise ValueError("allowed_tools 不能重复")
        for item in self.verifier_spec.assertions:
            if item.expected_key not in self.expected_outcome:
                raise ValueError("断言引用了不存在的 expected_outcome 字段")
        return self

    def agent_visible(self) -> AgentVisibleTask:
        return AgentVisibleTask(user_query=self.user_query, allowed_tools=self.allowed_tools,
                                execution_budget=self.execution_budget)

    def digest(self) -> str:
        return sha256(canonical_json(self).encode("utf-8")).hexdigest()


class ArtifactReference(ContractModel):
    """相对于 Trial 根目录的文件引用；禁止绝对路径和父目录跳转。"""

    path: Nonempty
    sha256: Digest | None = None

    @field_validator("path")
    @classmethod
    def safe_relative_path(cls, value):
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or "\\" in value or ":" in value:
            raise ValueError("Artifact 必须是 Trial 根目录内的相对路径")
        if value != path.as_posix() or value == ".":
            raise ValueError("Artifact 路径必须规范化并指向文件")
        return value


class SessionArtifacts(ContractModel):
    session_id: Identifier
    transcript: ArtifactReference
    run_reports: tuple[ArtifactReference, ...]
    # 可为空，明确表示尚未采集；不得据此推算真实成本为零。
    provider_usage: tuple[ArtifactReference, ...]

    @model_validator(mode="after")
    def session_paths(self):
        prefix = f"sessions/{self.session_id}/"
        references = (self.transcript, *self.run_reports, *self.provider_usage)
        if any(not reference.path.startswith(prefix) for reference in references):
            raise ValueError("Session Artifact 必须位于对应 sessions/<session_id>/ 目录")
        return self


class AssertionResult(ContractModel):
    assertion_id: Identifier
    category: Literal["outcome", "safety", "protocol", "trace"]
    status: Literal["PASS", "FAIL", "MISSING"]
    evidence: tuple[ArtifactReference, ...]
    detail: Nonempty


class VerifierResult(VersionedModel):
    suite_id: Identifier
    case_id: Identifier
    trial_id: Identifier
    status: Literal["PASS", "FAIL", "INVALID", "INCONCLUSIVE"]
    assertions: tuple[AssertionResult, ...]
    failure_category: Literal["outcome", "safety", "environment", "protocol",
                              "missing_evidence", "attribution"] | None
    verifier_version: Literal["1"]

    @model_validator(mode="after")
    def consistent_status(self):
        if (self.status == "PASS") != (self.failure_category is None):
            raise ValueError("PASS 不得带失败分类，其他状态必须带分类")
        if self.status == "PASS" and (not self.assertions or
                                      any(a.status != "PASS" for a in self.assertions)):
            raise ValueError("PASS 必须有全部通过的断言")
        categories = {"FAIL": {"outcome", "safety"}, "INVALID": {"environment", "protocol"},
                      "INCONCLUSIVE": {"missing_evidence", "attribution"}}
        if self.status in categories and self.failure_category not in categories[self.status]:
            raise ValueError("结果状态与失败分类不一致")
        return self


class TrialManifest(VersionedModel):
    suite_id: Identifier
    task_id: Identifier
    case_id: Identifier
    trial_id: Identifier
    environment_kind: Literal["hexworkspace", "business_fixture"]
    phase: Literal["protocol_smoke", "baseline", "ablation"]
    baseline_id: Identifier
    verifier_version: Literal["1"]
    task_contract_sha256: Digest
    contract: ArtifactReference
    run_config: dict[str, JsonValue]
    code_versions: dict[str, Nonempty] = Field(min_length=1)
    sessions: tuple[SessionArtifacts, ...] = Field(min_length=1)
    verifier_result: VerifierResult | None = None

    @model_validator(mode="after")
    def validate_identity(self):
        ids = [session.session_id for session in self.sessions]
        if len(ids) != len(set(ids)):
            raise ValueError("Trial 中 Session ID 不能重复")
        if self.verifier_result is not None:
            result = self.verifier_result
            if (result.suite_id, result.case_id, result.trial_id) != (
                    self.suite_id, self.case_id, self.trial_id):
                raise ValueError("Verifier Result 与 Trial 标识不一致")
        return self

    def matches(self, contract: TaskContract) -> bool:
        return (self.suite_id, self.task_id, self.case_id, self.environment_kind,
                self.baseline_id, self.verifier_version, self.task_contract_sha256) == (
                    contract.suite_id, contract.task_id, contract.case_id,
                    contract.environment_kind, contract.baseline_id,
                    contract.verifier_spec.verifier_version, contract.digest())


def canonical_json(value: BaseModel) -> str:
    """稳定 JSON 同时用于写盘和冻结合同摘要；不序列化 SDK 对象。"""
    return json.dumps(value.model_dump(mode="json"), ensure_ascii=False,
                      sort_keys=True, separators=(",", ":"), allow_nan=False)
