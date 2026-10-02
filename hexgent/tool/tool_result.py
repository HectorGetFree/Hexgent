"""工具处理器和执行管线共享的结构化结果模型。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal


ToolOutputValidator = Callable[[dict[str, Any]], str | None]


ToolFailurePhase = Literal[
    "lookup",  # 注册表中查找工具。
    "validation",  # 解析和校验工具输入参数。
    "eligibility",  # Workspace Capability 不属于当前 Run 的冻结范围。
    "permission",  # 权限规则判断或用户审批。
    "scheduling",  # 批次预算、排队和并行调度。
    "execution",  # 实际调用工具处理器。
    "output_validation",  # 校验工具返回值和输出协议。
    "persistence",  # 外置工具结果或记录持久化。
    "runtime",  # 进程或运行环境异常中断。
]
ToolFailureCode = str
SideEffectState = Literal[
    "none",  # 当前工具不产生外部副作用。
    "not_started",  # 副作用工具尚未开始执行。
    "committed",  # 副作用已经成功提交。
    "unknown",  # 执行中断，无法确定副作用是否已经发生。
]
ToolExecutionStatus = Literal[
    "success",  # 工具调用已经成功执行并生成结果。
    "invalid",  # 工具不存在，或调用参数未通过解析与合法性校验。
    "denied",  # 权限管线或用户确认拒绝执行工具。
    "error",  # 工具执行期间发生异常或超时。
    "deduplicated",  # 相同工具及参数已成功执行，本次复用此前结果引用。
]


@dataclass(frozen=True)
class ToolFailure:
    """一次工具失败的稳定机器可读描述。"""

    # 用于分支判断、统计和恢复策略的稳定错误编码。
    code: ToolFailureCode

    # 错误发生在工具调用管线的具体阶段。
    phase: ToolFailurePhase

    # 适合返回给模型和用户的中文错误说明。
    message: str

    # 相同调用是否可以由执行器安全地自动重试。
    retryable: bool = False

    # 副作用是否开始、提交或处于无法确定的状态。
    side_effect_state: SideEffectState = "none"

    # 外部依赖建议的最短重试等待秒数；没有建议时为空。
    retry_after_seconds: float | None = None


@dataclass(frozen=True)
class ToolExecutionResult:
    """一次工具调用经过完整管线后形成的稳定结果。"""

    # 工具调用经过完整执行管线后得到的状态。
    status: ToolExecutionStatus
    # 返回给模型的工具执行文本。
    output: str
    # 标识该结果是否应被模型视为错误结果。
    is_error: bool
    # 去重命中时指向首次真实执行工具的调用标识。
    source_call_id: str | None = None
    # 精确去重使用的规范化工具调用指纹。
    fingerprint: str | None = None
    # 失败结果的稳定机器可读原因；成功结果为空。
    failure: ToolFailure | None = None
    # 包含首次执行在内，当前结果实际经过的处理器执行次数。
    attempt_count: int = 1
    # 已通过工具输出 Schema 的轻量结构化成功数据。
    data: dict[str, Any] | None = None


def build_tool_failure_result(
    status: ToolExecutionStatus,
    code: ToolFailureCode,
    phase: ToolFailurePhase,
    message: str,
    *,
    retryable: bool = False,
    side_effect_state: SideEffectState = "none",
    retry_after_seconds: float | None = None,
) -> ToolExecutionResult:
    """构造兼容工具执行状态协议的结构化失败结果。"""
    failure = ToolFailure(
        code,
        phase,
        message,
        retryable,
        side_effect_state,
        retry_after_seconds,
    )
    return ToolExecutionResult(status, f"错误：{message}", True, failure=failure)


@dataclass(frozen=True)
class ToolHandlerResult:
    """工具函数向统一执行管线返回的业务结果。"""

    # 工具业务操作是否成功完成。
    ok: bool

    # 成功正文或适合模型理解的失败说明。
    output: str

    # 业务失败的结构化原因；成功时必须为空。
    failure: ToolFailure | None = None

    # 按工具输出 Schema 校验的轻量 JSON 数据；不得重复保存大正文。
    data: dict[str, Any] | None = None

    # 处理器需要保留的最终工具状态；未设置时由统一执行器按失败语义推导。
    execution_status: ToolExecutionStatus | None = None

    @classmethod
    def success(
        cls,
        output: str,
        data: dict[str, Any] | None = None,
    ) -> "ToolHandlerResult":
        """构造业务成功结果。"""
        return cls(True, output, None, data, "success")

    @classmethod
    def failed(
        cls,
        code: ToolFailureCode,
        message: str,
        *,
        phase: ToolFailurePhase = "execution",
        retryable: bool = False,
        side_effect_state: SideEffectState = "none",
        retry_after_seconds: float | None = None,
        execution_status: ToolExecutionStatus | None = None,
    ) -> "ToolHandlerResult":
        """构造带有稳定失败元数据的业务失败结果。"""
        return cls(False, f"错误：{message}", ToolFailure(
            code, phase, message, retryable, side_effect_state, retry_after_seconds
        ), None, execution_status)


def validate_json_output(data: Any, schema: dict[str, Any]) -> str | None:
    """校验工具结构化输出是否满足当前支持的 JSON Schema 子集。"""
    return _validate_json_value(data, schema, "data")


def _validate_json_value(
    value: Any,
    schema: dict[str, Any],
    path: str,
) -> str | None:
    """递归校验一个 JSON 值，并返回首个稳定中文错误。"""
    expected_type = schema.get("type")
    if isinstance(expected_type, list):
        if not any(_matches_json_output_type(value, item) for item in expected_type):
            return f"{path} 必须是 {expected_type} 类型"
    elif isinstance(expected_type, str) and not _matches_json_output_type(
        value, expected_type
    ):
        return f"{path} 必须是 {expected_type} 类型"

    allowed_values = schema.get("enum")
    if isinstance(allowed_values, list) and value not in allowed_values:
        return f"{path} 必须是以下值之一：{allowed_values}"

    if isinstance(value, dict):
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        missing = [name for name in required if name not in value]
        if missing:
            return f"{path} 缺少字段：{', '.join(missing)}"
        if schema.get("additionalProperties") is False:
            extra = [name for name in value if name not in properties]
            if extra:
                return f"{path} 包含未声明字段：{', '.join(extra)}"
        for name, item in value.items():
            property_schema = properties.get(name)
            if property_schema is None:
                continue
            error = _validate_json_value(item, property_schema, f"{path}.{name}")
            if error is not None:
                return error

    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for index, item in enumerate(value):
            error = _validate_json_value(item, schema["items"], f"{path}[{index}]")
            if error is not None:
                return error
    return None


def _matches_json_output_type(value: Any, expected_type: str) -> bool:
    """判断 Python 值是否属于基础 JSON 类型。"""
    if expected_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    type_mapping: dict[str, type] = {
        "string": str,
        "boolean": bool,
        "object": dict,
        "array": list,
        "null": type(None),
    }
    expected_python_type = type_mapping.get(expected_type)
    return expected_python_type is None or isinstance(value, expected_python_type)
