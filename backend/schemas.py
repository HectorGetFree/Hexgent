"""Web API 的请求与响应模型；它们不进入 Agent 领域层。"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from hexgent.tool.tool_permission_pipeline import PermissionMode


RunStatus = Literal["pending", "running", "paused", "cancelled", "completed", "failed"]


class StrictModel(BaseModel):
    """拒绝前端拼错但后端并未消费的字段。"""

    model_config = ConfigDict(extra="forbid")


class CreateSessionRequest(StrictModel):
    """创建 Session 时允许浏览器提交的参数。"""
    workdir: str | None = None
    permission_mode: PermissionMode = "default"


class SessionResponse(StrictModel):
    """Session 查询与创建接口的统一响应。"""
    session_id: str
    workdir: str
    permission_mode: PermissionMode
    active_run_id: str | None
    recent_run_ids: list[str] = Field(default_factory=list)
    plan: dict[str, Any] | None = None


class CreateRunRequest(StrictModel):
    """启动一次新 Agent Run 的请求。"""
    query: str

    @field_validator("query")
    @classmethod
    def query_must_not_be_blank(cls, value: str) -> str:
        """拒绝只包含空白字符的初始 Query。"""
        if not value.strip():
            raise ValueError("Query 不能为空")
        return value


class RunResponse(StrictModel):
    """Run 创建与状态查询接口的统一响应。"""
    run_id: str
    session_id: str
    status: RunStatus
    created_at: datetime
    finished_at: datetime | None
    error: str | None


class ControlResponse(StrictModel):
    """Pause 或 Cancel 控制请求已被接受的响应。"""
    run_id: str
    accepted: bool = True
    action: Literal["pause", "cancel"]


class SteerRequest(StrictModel):
    """向活跃 Run 提交的 Steering 请求。"""
    query: str

    @field_validator("query")
    @classmethod
    def query_must_not_be_blank(cls, value: str) -> str:
        """拒绝只包含空白字符的 Steering Query。"""
        if not value.strip():
            raise ValueError("Steering Query 不能为空")
        return value


class SteerResponse(StrictModel):
    """Steering Query 入队后的 Run 内序号响应。"""
    run_id: str
    steering_sequence: int


class PermissionDecisionRequest(StrictModel):
    """前端对一条权限请求作出的允许或拒绝决定。"""
    decision: Literal["allow", "deny"]


class PermissionDecisionResponse(StrictModel):
    """权限决定已经提交到等待线程的响应。"""
    request_id: str
    decision: Literal["allow", "deny"]
