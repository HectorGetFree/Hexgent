"""Run 查询、控制、审批和 SSE 接口。"""

from fastapi import APIRouter, Depends, Header
from fastapi.responses import StreamingResponse

from ..dependencies import get_run_manager
from ..errors import WebError
from ..run_manager import RunManager
from ..schemas import ControlResponse, PermissionDecisionRequest, PermissionDecisionResponse, RunResponse, SteerRequest, SteerResponse

router = APIRouter(prefix="/api/runs", tags=["runs"])


@router.get("/{run_id}", response_model=RunResponse)
async def get_run(run_id: str, manager: RunManager = Depends(get_run_manager)):
    """查询 Run 的当前状态、时间和后台错误。"""
    return manager.run_dict(await manager.get_run(run_id))


@router.post("/{run_id}/steer", response_model=SteerResponse)
async def steer_run(run_id: str, body: SteerRequest, manager: RunManager = Depends(get_run_manager)):
    """向运行中的 Run 提交一条保持原文的 FIFO Steering Query。"""
    query = await manager.steer_run(run_id, body.query)
    return {"run_id": run_id, "steering_sequence": query.sequence}


@router.post("/{run_id}/pause", response_model=ControlResponse, status_code=202)
async def pause_run(run_id: str, manager: RunManager = Depends(get_run_manager)):
    """接受安全暂停请求；实际 paused 状态由 RunFinishedEvent 确认。"""
    await manager.pause_run(run_id)
    return {"run_id": run_id, "action": "pause", "accepted": True}


@router.post("/{run_id}/cancel", response_model=ControlResponse, status_code=202)
async def cancel_run(run_id: str, manager: RunManager = Depends(get_run_manager)):
    """接受安全取消请求；不会回滚已经完成的外部副作用。"""
    await manager.cancel_run(run_id)
    return {"run_id": run_id, "action": "cancel", "accepted": True}


@router.post("/{run_id}/permissions/{request_id}", response_model=PermissionDecisionResponse)
async def decide_permission(run_id: str, request_id: str, body: PermissionDecisionRequest, manager: RunManager = Depends(get_run_manager)):
    """批准或拒绝目标 Run 中正在等待的工具权限请求。"""
    request = await manager.decide_permission(run_id, request_id, body.decision)
    return {"request_id": request.request_id, "decision": request.decision}


@router.get("/{run_id}/events")
async def events(run_id: str, last_event_id: str | None = Header(default=None, alias="Last-Event-ID"), manager: RunManager = Depends(get_run_manager)):
    """回放缓存事件并持续输出目标 Run 的 SSE 事件流。"""
    # 1. 浏览器重连时从 Last-Event-ID 的下一条事件继续回放。
    try:
        cursor = int(last_event_id) if last_event_id else 0
    except ValueError as error:
        raise WebError(400, "invalid_last_event_id", "Last-Event-ID 必须是整数") from error
    # 2. 在返回 StreamingResponse 前先验证 Run，确保 404 仍是普通 JSON 响应。
    await manager.get_run(run_id)
    # 3. 禁止代理缓存和缓冲，确保增量事件尽快到达前端。
    return StreamingResponse(manager.stream_events(run_id, cursor), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
