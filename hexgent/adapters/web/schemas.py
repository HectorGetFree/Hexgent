"""Web Adapter 对外暴露的请求与响应协议。"""

from typing import Literal

from pydantic import BaseModel, Field

from ...tool.tool_permission_pipeline import PermissionMode


RunStatus = Literal[
    "pending",  # Run 已创建，后台工作线程尚未开始。
    "running",  # Agent Core 正在执行。
    "paused",  # 用户暂停，Context、Session 和 Plan 仍然保留。
    "cancelled",  # 用户取消，旧 Plan 的继续执行义务已经终结。
    "completed",  # Agent Core 正常完成。
    "blocked",  # Context 或 Plan 阻塞，需要用户补充信息。
    "incomplete",  # 当前 Run 已停止，但用户目标尚未完成。
    "limit_reached",  # 当前 Run 命中预算并完成收尾。
    "failed",  # Adapter 或 Core 发生未处理异常。
]


class CreateSessionRequest(BaseModel):
    """创建一个隔离 AgentEngine 会话。"""

    # 当前 Session 使用的权限模式；工作区由服务端启动配置决定。
    permission_mode: PermissionMode = "default"


class SessionResponse(BaseModel):
    """可提供给网络调用方的 Session 状态。"""

    session_id: str
    permission_mode: PermissionMode
    active_run_id: str | None


class StartRunRequest(BaseModel):
    """启动一次 Agent Run。"""

    # 必须保持原文传入 Agent Core 的用户 Query。
    query: str = Field(min_length=1)


class SteeringRequest(BaseModel):
    """向当前活跃 Run 的 FIFO 提交一条改向要求。"""

    query: str = Field(min_length=1)


class PermissionDecisionRequest(BaseModel):
    """回应一次由工具权限管线发起的 Web 审批。"""

    decision: Literal["allow", "deny"]


class RunResponse(BaseModel):
    """可提供给网络调用方的 Run 生命周期状态。"""

    run_id: str
    session_id: str
    query: str
    status: RunStatus
    created_at: str
    finished_at: str | None
    error: str | None


class ControlResponse(BaseModel):
    """控制请求被 Adapter 接受后的确认。"""

    run_id: str
    accepted: bool = True
    action: Literal["pause", "cancel", "steer", "permission"]
