"""工具处理器和执行管线共享的结构化结果模型。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


ToolFailurePhase = Literal[
    "lookup",  # 注册表中查找工具。
    "validation",  # 解析和校验工具输入参数。
    "permission",  # 权限规则判断或用户审批。
    "scheduling",  # 批次预算、排队和并行调度。
    "execution",  # 实际调用工具处理器。
    "output_validation",  # 校验工具返回值和输出协议。
    "persistence",  # 外置工具结果或记录持久化。
    "runtime",  # 进程或运行环境异常中断。
]
ToolFailureCode = Literal[
    "unknown_tool",  # 模型请求了注册表中不存在的工具。
    "invalid_arguments",  # 工具参数无法解析或不满足输入约束。
    "permission_denied",  # 系统权限策略直接拒绝调用。
    "user_denied",  # 用户明确拒绝本次审批。
    "confirmation_unavailable",  # 当前入口无法完成需要的用户审批。
    "tool_timeout",  # 工具或并行批次超过执行时间限制。
    "tool_internal_error",  # 工具实现抛出未预期的内部异常。
    "external_dependency_failed",  # 文件系统、进程或外部服务调用失败。
    "rate_limited",  # 外部依赖触发限流，可按服务端建议时间重试。
    "invalid_tool_output",  # 工具返回值不符合处理器输出协议。
    "business_failure",  # 工具技术执行完成，但业务操作没有成功。
    "duplicate_call_limit",  # 相同工具调用超过当前 Run 的复用次数限制。
    "output_persistence_failed",  # 工具成功后，其完整结果无法可靠持久化。
]
SideEffectState = Literal[
    "none",  # 当前工具不产生外部副作用。
    "not_started",  # 副作用工具尚未开始执行。
    "committed",  # 副作用已经成功提交。
    "unknown",  # 执行中断，无法确定副作用是否已经发生。
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

    @classmethod
    def success(
        cls,
        output: str,
        data: dict[str, Any] | None = None,
    ) -> "ToolHandlerResult":
        """构造业务成功结果。"""
        return cls(True, output, None, data)

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
    ) -> "ToolHandlerResult":
        """构造带有稳定失败元数据的业务失败结果。"""
        return cls(
            False,
            f"错误：{message}",
            ToolFailure(
                code,
                phase,
                message,
                retryable,
                side_effect_state,
                retry_after_seconds,
            ),
        )
